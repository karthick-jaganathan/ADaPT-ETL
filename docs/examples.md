---
layout: page
title: Examples
nav_order: 4
description: "Practical examples and tutorials for ADaPT"
permalink: /examples/
---

# Examples and Tutorials

This section provides practical examples and tutorials for using ADaPT in various scenarios.

## 🚀 Quick Start Examples

### Basic Pipeline Execution

```bash
# Set environment variables
export ADAPT_CONFIGS="$(pwd)/configs"
export ADAPT_OUTPUT_DIR="/data/adapt_etl"

# Run a basic pipeline
adapt_pipeline --namespace your_service \
  --pipeline-config data_ingestion.yaml \
  --data-ingestion-config api_config.yaml \
  --auth-data api_key="your-api-key" \
  --external-input resource_id="123456"
```

### Google Ads Integration

```bash
adapt_pipeline --namespace google \
  --pipeline-config data_ingestion.yaml \
  --data-ingestion-config campaign.yaml \
  --auth-data client_id="your-client-id" \
  --auth-data client_secret="your-client-secret" \
  --auth-data developer_token="your-developer-token" \
  --auth-data login_customer_id="your-login-customer-id" \
  --auth-data refresh_token="your-refresh-token" \
  --external-input customer_id="your-customer-id" \
  --external-input advertising_channel_type="SEARCH,SHOPPING"
```

### Facebook Ads Integration

```bash
adapt_pipeline --namespace facebook \
  --pipeline-config data_ingestion.yaml \
  --data-ingestion-config campaign.yaml \
  --auth-data refresh_token="your-access-token" \
  --auth-data account_id="act_your-account-id" \
  --auth-data consumer_key="your-consumer-key" \
  --auth-data consumer_secret="your-consumer-secret" \
  --external-input campaign_ids="123,456,789"
```

## 📊 Data Transformation Examples

### Basic Field Mapping

```yaml
# configs/serializer/your_service/basic_transform.yaml
version: 0.1
kind: serializer

inline:
  - name: entity_id
    from: id
    transform:
      type: integer
  
  - name: entity_name
    from: name
    transform:
      type: string
  
  - name: status_active
    from: status
    transform:
      type: boolean
```

### Advanced Transformations with Conditional Logic

```yaml
# configs/serializer/your_service/advanced_transform.yaml
version: 0.1
kind: serializer

inline:
  - name: campaign_id
    from: id
    transform:
      type: integer
  
  - name: campaign_name
    from: name
    transform:
      type: string
  
  - name: status_label
    transform:
      type: case
      cases:
        - when:
            field: status
            equals: "ENABLED"
          then: "Active"
        - when:
            field: status
            equals: "PAUSED"
          then: "Paused"
        - when:
            field: status
            equals: "REMOVED"
          then: "Deleted"
      default: "Unknown"
  
  - name: priority_level
    from: priority
    transform:
      type: enum
      mapping:
        1: "Low"
        2: "Medium"
        3: "High"
        4: "Critical"
      default: "Unknown"
  
  - name: budget_category
    transform:
      type: case
      cases:
        - when:
            field: daily_budget_micros
            greater_than: 100000000  # $100
          then: "High Budget"
        - when:
            field: daily_budget_micros
            greater_than: 50000000   # $50
          then: "Medium Budget"
      default: "Low Budget"

constants:
  - name: data_source
    value: "API"
    transform:
      type: string
  
  - name: extraction_date
    value: "2024-01-01"
    transform:
      type: string
```

### Nested Object Processing

```yaml
# configs/serializer/your_service/nested_transform.yaml
version: 0.1
kind: serializer

inline:
  - name: campaign_id
    object: campaign
    from: id
    transform:
      type: integer
  
  - name: campaign_name
    object: campaign
    from: name
    transform:
      type: string
  
  - name: impressions
    object: metrics
    from: impressions
    transform:
      type: integer
  
  - name: clicks
    object: metrics
    from: clicks
    transform:
      type: integer
  
  - name: cost_micros
    object: metrics
    from: cost_micros
    transform:
      type: integer

derived:
  - name: click_through_rate
    transform:
      type: case
      cases:
        - when:
            field: impressions
            greater_than: 0
          then:
            type: formula
            expression: "clicks / impressions * 100"
      default: 0.0
  
  - name: cost_per_click
    transform:
      type: case
      cases:
        - when:
            field: clicks
            greater_than: 0
          then:
            type: formula
            expression: "cost_micros / clicks / 1000000"
      default: 0.0
```

## 🔧 Configuration Examples

### Pipeline Configuration

```yaml
# configs/pipeline/data_ingestion.yaml
version: 1.0
kind: pipeline
description: "Generic data ingestion pipeline"

pipeline:
  type: list
  items:
    - name: connector_config
      client:
        type: callable
        module: adapt.utils.config_reader
        class: YamlReader
        method: load_from_config_location
      arguments:
        type: dict
        items:
          module:
            type: constant
            value: connector
          namespace:
            type: external_input
            key: namespace
          config_name:
            type: external_input
            key: data_ingestion_config
    
    - name: service
      client:
        type: callable
        module: adapt.connector.service
        class: Service
        method: initialize
      arguments:
        type: dict
        items:
          config:
            type: external_input
            key: connector_config
          external_input:
            type: external_input
            key: external_input
    
    - name: serializer_config
      client:
        type: callable
        module: adapt.utils.config_reader
        class: YamlReader
        method: load_from_config_location
      arguments:
        type: dict
        items:
          module:
            type: constant
            value: serializer
          namespace:
            type: external_input
            key: namespace
          config_name:
            type: external_input
            key: data_ingestion_config
    
    - name: serializer
      client:
        type: callable
        module: adapt.serializer.serializer
        class: Serializer
        method: init
      arguments:
        type: dict
        items:
          config:
            type: external_input
            key: serializer_config
    
    - name: exporter
      client:
        type: callable
        module: adapt.utils.exporter
        class: CSVExporter
        method: lazy_run
      arguments:
        type: dict
        items:
          config:
            type: external_input
            key: serializer_config
          records:
            type: callable
            module: adapt.serializer.serializer
            class: Serializer
            method: serialize_records
            arguments:
              type: dict
              items:
                records:
                  type: external_input
                  key: service
```

### Connector Configuration

```yaml
# configs/connector/your_service/api_config.yaml
version: 1.0
kind: connector
description: "Generic API connector configuration"

authorization:
  namespace: your_service
  config_name: authorization.yaml

client:
  type: from_authorizer
  method: get_client
  arguments:
    type: dict
    items:
      service_name:
        type: constant
        value: YourAPIService

dispatcher:
  type: instance
  module: adapt.connector.dispatcher
  class: Dispatcher
  arguments:
    type: dict
    items:
      client:
        type: external_input
        key: client
      requests:
        type: list
        items:
          - type: dict
            items:
              endpoint:
                type: constant
                value: "/api/v1/data"
              method:
                type: constant
                value: "GET"
              params:
                type: dict
                items:
                  resource_id:
                    type: external_input
                    key: resource_id
                  limit:
                    type: constant
                    value: 1000

post_processor:
  type: instance
  module: your_service.post_processor
  class: DataProcessor
  arguments:
    type: dict
    items:
      stream:
        type: external_input
        key: dispatcher
```

### Authorization Configuration

```yaml
# configs/authorization/your_service/authorization.yaml
version: 1.0
kind: authorization
description: "API authorization configuration"

authorizer:
  type: instance
  module: adapt.connector.authorization
  class: Authorizer
  arguments:
    type: dict
    items:
      auth_type:
        type: constant
        value: "oauth2"
      credentials:
        type: dict
        items:
          client_id:
            type: external_input
            key: client_id
          client_secret:
            type: external_input
            key: client_secret
          access_token:
            type: external_input
            key: access_token
          refresh_token:
            type: external_input
            key: refresh_token
```

## 🐍 Python API Examples

### Basic Usage

```python
from adapt.utils import Store, config_reader
from adapt.connector.service import Service
from adapt.serializer.serializer import Serializer
from adapt.utils.exporter import CSVExporter

# Set up external input
external_input = Store()
external_input.add('resource_id', '123456789')
external_input.add('entity_ids', '111,222,333')

# Load connector configuration
connector_config = config_reader.YamlReader.load_from_config_location(
    module="connector",
    namespace="your_service",
    config_name="api_config.yaml"
)

# Initialize service
service = Service.initialize(connector_config, external_input)

# Load serializer configuration
serializer_config = config_reader.YamlReader.load_from_config_location(
    module="serializer",
    namespace="your_service",
    config_name="data_transform.yaml"
)

# Initialize serializer
serializer = Serializer.init(serializer_config)

# Transform data
transformed_data = list(serializer.serialize_records(service))

# Export to CSV
file_path = CSVExporter.lazy_run(serializer_config, transformed_data)
print(f"Data exported to: {file_path}")
```

### Custom Data Processing

```python
from adapt.serializer.serializer import Serializer

# Define transformation configuration
config = {
    "inline": [
        {
            "name": "entity_id",
            "from": "id",
            "transform": {"type": "integer"}
        },
        {
            "name": "entity_name",
            "from": "name",
            "transform": {"type": "string"}
        },
        {
            "name": "status_label",
            "transform": {
                "type": "case",
                "cases": [
                    {
                        "when": {"field": "status", "equals": "ACTIVE"},
                        "then": "Active"
                    },
                    {
                        "when": {"field": "status", "equals": "INACTIVE"},
                        "then": "Inactive"
                    }
                ],
                "default": "Unknown"
            }
        }
    ]
}

# Create serializer
serializer = Serializer.init(config)

# Sample data
raw_data = [
    {"id": "123", "name": "Entity 1", "status": "ACTIVE"},
    {"id": "456", "name": "Entity 2", "status": "INACTIVE"},
    {"id": "789", "name": "Entity 3", "status": "PENDING"}
]

# Transform data
transformed_data = list(serializer.serialize_records(raw_data))
print(transformed_data)
# Output: [
#   {"entity_id": 123, "entity_name": "Entity 1", "status_label": "Active"},
#   {"entity_id": 456, "entity_name": "Entity 2", "status_label": "Inactive"},
#   {"entity_id": 789, "entity_name": "Entity 3", "status_label": "Unknown"}
# ]
```

## 🐳 Docker Examples

### Using Docker Compose

```bash
# Build and start container
docker-compose build
docker-compose up -d

# Execute pipeline in running container
docker-compose exec adapt-etl adapt_pipeline \
  --namespace your_service \
  --pipeline-config data_ingestion.yaml \
  --data-ingestion-config api_config.yaml \
  --auth-data api_key="your-api-key" \
  --external-input resource_id="123456"

# Run pipeline in new container (no need to start first)
docker-compose run --rm adapt-etl adapt_pipeline \
  --namespace your_service \
  --pipeline-config data_ingestion.yaml \
  --data-ingestion-config api_config.yaml \
  --auth-data api_key="your-api-key" \
  --external-input resource_id="123456"
```

### Using Docker Directly

```bash
# Build image
docker build -t adapt-etl .

# Run pipeline with volume mounts
docker run -it --rm \
  -v $(pwd)/configs:/configs:ro \
  -v /data/adapt_etl:/data/adapt_etl \
  -e ADAPT_CONFIGS=/configs \
  -e ADAPT_OUTPUT_DIR=/data/adapt_etl \
  adapt-etl adapt_pipeline \
  --namespace your_service \
  --pipeline-config data_ingestion.yaml \
  --data-ingestion-config api_config.yaml \
  --auth-data api_key="your-api-key" \
  --external-input resource_id="123456"
```

## 🔍 Troubleshooting Examples

### Common Issues and Solutions

#### Environment Variable Not Set
```bash
# Error: 'ADAPT_CONFIGS' path environment variable not set
export ADAPT_CONFIGS="$(pwd)/configs"
```

#### Missing Dependencies
```bash
# Error: ModuleNotFoundError: No module named 'adapt.utils'
cd ADaPT-ETL
make install
```

#### Configuration File Not Found
```bash
# Error: Configuration file not found
# Ensure your configs directory structure is correct:
ls -la configs/
# Should show: pipeline/, connector/, serializer/, authorization/
```

For more troubleshooting help, see the [Installation Guide]({{ site.baseurl }}/installation#troubleshooting).

## 📚 Additional Resources

- **[Package Documentation]({{ site.baseurl }}/packages/)** - Detailed package references
- **[API Reference]({{ site.baseurl }}/api-reference)** - Complete API documentation
- **[Installation Guide]({{ site.baseurl }}/installation)** - Installation and setup instructions
- **[GitHub Repository](https://github.com/karthick-jaganathan/ADaPT-ETL)** - Source code and issues 