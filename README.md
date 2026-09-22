# gestor-reservaciones
API CRUD RESERVACIONES
# gestor-reservaciones

API CRUD de reservaciones sobre AWS Lambda, DynamoDB, S3 y API Gateway.

## Arquitectura

- **Lambda** (`GestorReservaciones-CRUD`, Python 3.14) — lógica de la API
- **DynamoDB** — almacenamiento, partition key `reservation_id` (String)
- **S3** — destino de las exportaciones en JSON
- **API Gateway** — expone los endpoints, stage `dev`

## Variables de entorno

| Variable | Descripción |
|---|---|
| `TABLE_NAME` | Nombre de la tabla de DynamoDB |
| `BUCKET_NAME` | Bucket de S3 donde se guardan los exports |

## Endpoints

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/reservations` | Lista todas las reservaciones |
| GET | `/reservations/{id}` | Obtiene una reservación |
| POST | `/reservations` | Crea una reservación |
| PUT | `/reservations/{id}` | Actualiza los campos enviados |
| DELETE | `/reservations/{id}` | Elimina una reservación |
| POST | `/export` | Exporta a S3 las reservaciones confirmadas |

## Modelo de datos

```json
{
  "reservation_id": "uuid generado por el servidor",
  "recurso": "Sala de reuniones A",
  "fecha": "2026-09-20",
  "hora_inicio": "10:00",
  "hora_fin": "11:00",
  "cliente": "Prueba en vivo",
  "estado": "confirmada"
}
```

`recurso`, `fecha`, `hora_inicio`, `hora_fin` y `cliente` son obligatorios al crear.
`estado` es opcional y por defecto vale `pendiente`.

## Pruebas (PowerShell)

Sustituye `TU-URL` por el ID de tu API Gateway.

```powershell
$base = "https://TU-URL.execute-api.us-east-1.amazonaws.com/dev"

# Listar
Invoke-RestMethod -Uri "$base/reservations" -Method GET

# Crear
$body = '{"recurso": "Sala de reuniones A", "fecha": "2026-09-20", "hora_inicio": "10:00", "hora_fin": "11:00", "cliente": "Prueba en vivo", "estado": "confirmada"}'
Invoke-RestMethod -Uri "$base/reservations" -Method POST -ContentType "application/json" -Body $body

# Obtener
Invoke-RestMethod -Uri "$base/reservations/EL-ID-AQUI" -Method GET

# Actualizar
$body = '{"estado": "cancelada"}'
Invoke-RestMethod -Uri "$base/reservations/EL-ID-AQUI" -Method PUT -ContentType "application/json" -Body $body

# Eliminar
Invoke-RestMethod -Uri "$base/reservations/EL-ID-AQUI" -Method DELETE

# Exportar
Invoke-RestMethod -Uri "$base/export" -Method POST
```

## Permisos del rol de ejecución

Además de `AWSLambdaBasicExecutionRole`, el rol necesita `dynamodb:GetItem`, `PutItem`, `UpdateItem`, `DeleteItem` y `Scan` sobre la tabla, y `s3:PutObject` sobre el bucket.
