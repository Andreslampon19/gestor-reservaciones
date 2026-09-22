import os
import json
import uuid
import base64
from decimal import Decimal
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

TABLE_NAME = os.environ["TABLE_NAME"]
BUCKET_NAME = os.environ["BUCKET_NAME"]

table = boto3.resource("dynamodb").Table(TABLE_NAME)
s3 = boto3.client("s3")

# Único punto donde se decide qué puede cambiar un PUT: impide sobreescribir reservation_id o creado_en
CAMPOS_EDITABLES = ["recurso", "fecha", "hora_inicio", "hora_fin", "cliente", "estado"]
CAMPOS_OBLIGATORIOS = ["recurso", "fecha", "hora_inicio", "hora_fin", "cliente"]

ESTADOS_VALIDOS = ["pendiente", "confirmada", "cancelada"]


def serializar(obj):
    # DynamoDB devuelve números como Decimal y json.dumps no sabe qué hacer con eso
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    return str(obj)


def respuesta(status, cuerpo):
    # API Gateway exige estos tres campos; si falta alguno responde 502 sin explicar nada
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(cuerpo, ensure_ascii=False, default=serializar),
    }


def leer_body(event):
    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return json.loads(body)


def resolver_ruta(event):
    # HTTP API v2 trae routeKey listo; REST API v1 obliga a armarlo a mano
    if event.get("routeKey"):
        return event["routeKey"]

    metodo = event.get("httpMethod") or event["requestContext"]["http"]["method"]
    ruta = event.get("resource") or event.get("path") or ""
    # El stage viaja en el path cuando se usa la URL de invocación directa
    stage = event.get("requestContext", {}).get("stage")
    if stage and ruta.startswith(f"/{stage}"):
        ruta = ruta[len(stage) + 1:]
    return f"{metodo} {ruta}"


def listar(query):
    # Un scan con filtro cubre el requisito "query o scan que no sea solo por ID"
    filtros = None
    for campo in ("recurso", "fecha", "estado"):
        valor = query.get(campo)
        if valor:
            condicion = Attr(campo).eq(valor)
            filtros = condicion if filtros is None else filtros & condicion

    resultado = table.scan(FilterExpression=filtros) if filtros else table.scan()
    items = resultado.get("Items", [])
    items.sort(key=lambda r: (r.get("fecha", ""), r.get("hora_inicio", "")))
    return respuesta(200, {"total": len(items), "items": items})


def obtener(reservation_id):
    resultado = table.get_item(Key={"reservation_id": reservation_id})
    if "Item" not in resultado:
        return respuesta(404, {"error": f"No existe la reservación {reservation_id}"})
    return respuesta(200, resultado["Item"])


def buscar_traslape(recurso, fecha, hora_inicio, hora_fin, excluir_id=None):
    # Dos rangos se traslapan si cada uno empieza antes de que el otro termine
    resultado = table.scan(
        FilterExpression=Attr("recurso").eq(recurso)
        & Attr("fecha").eq(fecha)
        & Attr("estado").ne("cancelada")
        & Attr("hora_inicio").lt(hora_fin)
        & Attr("hora_fin").gt(hora_inicio)
    )
    for item in resultado.get("Items", []):
        if item["reservation_id"] != excluir_id:
            return item
    return None


def crear(event):
    datos = leer_body(event)

    faltantes = [c for c in CAMPOS_OBLIGATORIOS if not datos.get(c)]
    if faltantes:
        return respuesta(400, {"error": "Faltan campos obligatorios", "campos": faltantes})

    if datos["hora_fin"] <= datos["hora_inicio"]:
        return respuesta(400, {"error": "hora_fin debe ser posterior a hora_inicio"})

    estado = datos.get("estado", "pendiente")
    if estado not in ESTADOS_VALIDOS:
        return respuesta(400, {"error": f"Estado inválido. Permitidos: {ESTADOS_VALIDOS}"})

    conflicto = buscar_traslape(datos["recurso"], datos["fecha"],
                                datos["hora_inicio"], datos["hora_fin"])
    if conflicto:
        return respuesta(409, {
            "error": "El recurso ya está reservado en ese horario",
            "conflicto": conflicto,
        })

    item = {
        "reservation_id": str(uuid.uuid4()),
        "recurso": datos["recurso"],
        "fecha": datos["fecha"],
        "hora_inicio": datos["hora_inicio"],
        "hora_fin": datos["hora_fin"],
        "cliente": datos["cliente"],
        "estado": estado,
        "creado_en": datetime.now(timezone.utc).isoformat(),
    }
    table.put_item(Item=item)
    return respuesta(201, item)


def actualizar(reservation_id, event):
    datos = leer_body(event)
    cambios = {k: v for k, v in datos.items() if k in CAMPOS_EDITABLES}
    if not cambios:
        return respuesta(400, {"error": f"Nada que actualizar. Campos permitidos: {CAMPOS_EDITABLES}"})

    if "estado" in cambios and cambios["estado"] not in ESTADOS_VALIDOS:
        return respuesta(400, {"error": f"Estado inválido. Permitidos: {ESTADOS_VALIDOS}"})

    # Si cambia el horario o el recurso hay que revalidar contra las demás reservaciones
    if {"recurso", "fecha", "hora_inicio", "hora_fin"} & cambios.keys():
        actual = table.get_item(Key={"reservation_id": reservation_id}).get("Item")
        if not actual:
            return respuesta(404, {"error": f"No existe la reservación {reservation_id}"})

        futuro = {**actual, **cambios}
        if futuro["hora_fin"] <= futuro["hora_inicio"]:
            return respuesta(400, {"error": "hora_fin debe ser posterior a hora_inicio"})

        if futuro.get("estado") != "cancelada":
            conflicto = buscar_traslape(futuro["recurso"], futuro["fecha"],
                                        futuro["hora_inicio"], futuro["hora_fin"],
                                        excluir_id=reservation_id)
            if conflicto:
                return respuesta(409, {
                    "error": "El recurso ya está reservado en ese horario",
                    "conflicto": conflicto,
                })

    try:
        resultado = table.update_item(
            Key={"reservation_id": reservation_id},
            UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in cambios),
            ExpressionAttributeNames={f"#{k}": k for k in cambios},  # los alias evitan choques con palabras reservadas
            ExpressionAttributeValues={f":{k}": v for k, v in cambios.items()},
            ConditionExpression="attribute_exists(reservation_id)",  # sin esto, un PUT a un ID inexistente lo crearía a medias
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return respuesta(404, {"error": f"No existe la reservación {reservation_id}"})
        raise
    return respuesta(200, resultado["Attributes"])


def eliminar(reservation_id):
    table.delete_item(Key={"reservation_id": reservation_id})
    return respuesta(200, {"mensaje": "Reservación eliminada", "reservation_id": reservation_id})


def exportar():
    items = table.scan().get("Items", [])

    por_estado = {}
    por_recurso = {}
    for r in items:
        estado = r.get("estado", "sin estado")
        recurso = r.get("recurso", "sin recurso")
        por_estado[estado] = por_estado.get(estado, 0) + 1
        por_recurso[recurso] = por_recurso.get(recurso, 0) + 1

    reporte = {
        "generado_en": datetime.now(timezone.utc).isoformat(),
        "total_reservaciones": len(items),
        "por_estado": por_estado,
        "por_recurso": por_recurso,
        "reservaciones": items,
    }
    fecha = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = f"exports/reservaciones-{fecha}.json"
    s3.put_object(
        Bucket=BUCKET_NAME,
        Key=key,
        Body=json.dumps(reporte, ensure_ascii=False, default=serializar, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return respuesta(200, {"mensaje": "Exportación exitosa", "archivo": key, "total_registros": len(items)})


def lambda_handler(event, context):
    print(f"Evento recibido: {json.dumps(event)}")  # queda en CloudWatch Logs: primer lugar donde mirar si algo falla
    ruta = resolver_ruta(event)                     # "GET /reservations/{id}" en ambas versiones de API Gateway
    params = event.get("pathParameters") or {}
    query = event.get("queryStringParameters") or {}

    try:
        if ruta == "GET /reservations":
            return listar(query)
        if ruta == "GET /reservations/{id}":
            return obtener(params["id"])
        if ruta == "POST /reservations":
            return crear(event)
        if ruta == "PUT /reservations/{id}":
            return actualizar(params["id"], event)
        if ruta == "DELETE /reservations/{id}":
            return eliminar(params["id"])
        if ruta == "POST /export":
            return exportar()
        return respuesta(404, {"error": f"Ruta no reconocida: {ruta}"})
    except json.JSONDecodeError:
        return respuesta(400, {"error": "El body no es JSON válido"})
    except Exception as e:  # se registra y responde 500 en vez de dejar que API Gateway devuelva 502
        print(f"Error no controlado: {e}")
        return respuesta(500, {"error": str(e)})
