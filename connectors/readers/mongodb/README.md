# MongoDB Connector for StreamWright

`streamwright-mongodb` is an official StreamWright connector for extracting documents from **MongoDB** collections and aggregation pipelines.

## Requirements

- Python >= 3.10
- `pymongo>=4.6.0`

## Installation

```bash
pip install streamwright-mongodb
```

## Authentication

Accepts either a standard connection URI or structured parameters:

### Connection URI (Secrets)
```yaml
auth:
  provider: mongodb
  uri: "{{ secrets.mongodb_uri }}" # e.g. mongodb+srv://user:pass@cluster.mongodb.net/dbname
```

### Structured Form
```yaml
auth:
  provider: mongodb
  host: "{{ config.host }}"
  port: 27017
  database: "{{ config.database }}"
  username: "{{ config.username }}"
  password: "{{ secrets.mongodb_password }}"
  auth_source: "admin"
```

## Supported Methods

| Method | Description | Arguments |
|---|---|---|
| `find` | Query collection documents | `collection`, `filter`, `projection`, `sort`, `limit`, `batch_size` |
| `aggregate` | Execute aggregation pipeline | `collection`, `pipeline`, `batch_size` |

### JSON Serialization
Automatically converts BSON types to standard JSON:
- `ObjectId` converted to `_id` string
- `datetime` converted to ISO 8601 UTC timestamp string
- `Decimal128` converted to `float` or exact string
