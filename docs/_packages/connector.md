---
layout: package
title: Connector Package
nav_order: 2
parent: Packages
description: "API connectors, authorization, and data extraction for the ADaPT ecosystem"
permalink: /packages/connector/
---

# ADaPT Connector - Data Source Integration Package

**API connectors, authorization, and data extraction for the ADaPT ecosystem.**

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/python-3.7+-blue.svg)](https://www.python.org/downloads/)

## 🚀 Overview

The `adapt-connector` package provides data source integration capabilities for the ADaPT (Adaptive Data Pipeline Toolkit) ecosystem. It handles API authentication, client initialization, request dispatching, and response post-processing for various data sources including REST APIs, GraphQL endpoints, databases, and custom integrations.

## 📦 Features

- **🔐 Authorization Management**: OAuth2, API key, and custom authentication flows
- **🌐 API Client Integration**: Seamless integration with REST APIs, GraphQL endpoints, databases, and custom data sources
- **📡 Request Dispatching**: Flexible request handling with method invocation and parameter management
- **🔄 Response Post-Processing**: Transform API responses into standardized formats
- **⚙️ Configuration-Driven**: Define connections and authentication through YAML configurations
- **🔌 Extensible Architecture**: Easy to add support for new APIs and authentication methods

## 🛠️ Installation

### Option 1: Install from Source (Recommended)
```bash
git clone https://github.com/karthick-jaganathan/ADaPT-ETL.git
cd ADaPT-ETL/adapt/connector
pip install .
```

This will automatically install all required dependencies:
- `adapt-utils` (Core utilities)

### Option 2: Development Installation
```bash
git clone https://github.com/karthick-jaganathan/ADaPT-ETL.git
cd ADaPT-ETL/adapt/connector
pip install -e .
```

### Option 3: As Part of Complete ADaPT Installation
```bash
git clone https://github.com/karthick-jaganathan/ADaPT-ETL.git
cd ADaPT-ETL
make install  # Installs all packages including connector
```

**Note**: If automatic dependency installation fails, install dependencies manually:
```bash
cd ../utils && pip install .
cd ../connector && pip install .
```

## 📋 Dependencies

This package depends on:
- **adapt-utils**: Core utilities (automatically installed)

Optional dependencies for specific integrations:
- `requests>=2.25.0` - For REST API integrations
- `sqlalchemy>=1.4.0` - For database connections
- `pymongo>=3.12.0` - For MongoDB integration
- `psycopg2-binary>=2.9.0` - For PostgreSQL connections

## ⚙️ Environment Variables

The connector package requires:

```bash
export ADAPT_CONFIGS=/path/to/your/configs
```

## 📚 Core Components

### Authorization

#### Authorization Class
Handles API authentication and client initialization:

```python
from adapt.connector.authorization import Authorization
from adapt.utils import Store

# Set up authentication data
auth_store = Store()
auth_store.add('api_key', 'your-api-key')
auth_store.add('api_secret', 'your-api-secret')
auth_store.add('access_token', 'your-access-token')

# Initialize from configuration file
auth_client = Authorization.from_config_path(
    'configs/authorization/your_service/authorization.yaml',
    auth_store
)
```

#### Authorization Configuration Examples

**OAuth2 API Authorization:**
```yaml
version: 1.0
kind: authorization
description: "OAuth2 API authentication"

initializer:
  type: initializer
  client:
    type: callable
    module: your_api_client.client
    class: APIClient
    method: from_credentials
  arguments:
    type: dict
    items:
      api_version:
        type: constant
        value: v1
      credentials:
        type: dict
        items:
          client_id:
            type: external_input
            key: client_id
            required: true
          client_secret:
            type: external_input
            key: client_secret
            required: true
          access_token:
            type: external_input
            key: access_token
            required: true
```

**API Key Authorization:**
```yaml
version: 1.0
kind: authorization
description: "API Key authentication"

initializer:
  type: initializer
  client:
    type: callable
    module: requests
    class: Session
    method: __init__
  arguments:
    type: dict
    items:
      headers:
        type: dict
        items:
          Authorization:
            type: external_input
            key: api_key
            required: true
            format: "Bearer {value}"
          Content-Type:
            type: constant
            value: "application/json"
```

### Service

#### Service Class
Manages connector initialization and configuration:

```python
from adapt.connector.service import Service
from adapt.utils import Store

# Set up external inputs
external_input = Store()
external_input.add('resource_id', '123456789')
external_input.add('entity_ids', '111,222,333')

# Initialize service from configuration
service = Service.from_config_path(
    'configs/connector/your_service/data_config.yaml',
    external_input
)
```

#### Service Configuration Example

```yaml
version: 1.0
kind: connector
description: "Generic API connector"

# Reference to authorization configuration
authorization:
  namespace: your_service
  config_name: authorization.yaml

# Client configuration
client:
  type: from_authorizer
  method: get_client
  arguments:
    type: dict
    items:
      service_name:
        type: constant
        value: DataService

# Request method and arguments
method: fetch_data
arguments:
  type: dict
  items:
    resource_id:
      type: external_input
      key: resource_id
      required: true
    query:
      type: query_builder
      query:
        type: api_query
        endpoint: "/api/v1/data"
        method: "GET"
      filters:
        type: parameter_filter
        items:
          entity_ids:
            operator: IN
            value:
              type: external_input
              key: entity_ids
              split_on: ","
              format_as: LIST
```

### Dispatcher

#### Dispatcher Class
Handles API request execution and response processing:

```python
from adapt.connector.dispatcher import Dispatcher
from adapt.utils import Store

# Set up client and configuration
client = service  # From previous example
config = {
    "method": "search_stream",
    "arguments": {
        "type": "dict",
        "items": {
            "customer_id": {
                "type": "external_input",
                "key": "customer_id",
                "required": True
            }
        }
    }
}

external_input = Store()
external_input.add('customer_id', '123456789')

# Execute request
response = Dispatcher.receive(client, config, external_input)
```

### Post-Processing

#### ResponseProcessor
Converts API responses to standardized dictionaries:

```python
from adapt.connector.post_processor import ResponseProcessor

# Process API response
processed_data = list(ResponseProcessor.process(api_response))
```

## 🔧 Advanced Usage

### Custom API Integration

#### Step 1: Create Authorization Configuration

```yaml
# configs/authorization/custom_api/authorization.yaml
version: 1.0
kind: authorization
description: "Custom API authentication"

initializer:
  type: initializer
  client:
    type: instance
    module: your_api_package.client
    class: APIClient
  arguments:
    type: dict
    items:
      api_key:
        type: external_input
        key: api_key
        required: true
      base_url:
        type: external_input
        key: base_url
        required: true
      timeout:
        type: constant
        value: 30
```

#### Step 2: Create Connector Configuration

```yaml
# configs/connector/custom_api/data.yaml
version: 1.0
kind: connector
description: "Custom API connector"

authorization:
  namespace: custom_api
  config_name: authorization.yaml

client:
  type: from_authorizer
  method: get_client

method: fetch_data
arguments:
  type: dict
  items:
    endpoint:
      type: external_input
      key: endpoint
      required: true
    params:
      type: dict
      items:
        limit:
          type: external_input
          key: limit
        offset:
          type: external_input
          key: offset
```

#### Step 3: Use the Custom Connector

```python
from adapt.connector.service import Service
from adapt.utils import Store

# Set up authentication and parameters
external_input = Store()
external_input.add('api_key', 'your-api-key')
external_input.add('base_url', 'https://api.example.com')
external_input.add('endpoint', '/data')
external_input.add('limit', '100')

# Initialize and use the connector
service = Service.from_config_path(
    'configs/connector/custom_api/data.yaml',
    external_input
)
```

### Query Building

#### SQL Query Construction

```yaml
# GAQL (Google Ads Query Language) example
query:
  type: sql_query
  query: |
    SELECT
      campaign.id,
      campaign.name,
      campaign.status,
      metrics.impressions,
      metrics.clicks
    FROM campaign

filters:
  type: sql_filter
  items:
    campaign.id:
      operator: IN
      value:
        type: external_input
        key: campaign_ids
        split_on: ","
        format_as: INT_LIST
        ignore_if: null
    campaign.status:
      operator: IN
      value:
        type: constant
        value: ["ENABLED", "PAUSED"]
        format_as: SINGLE_QUOTED_LIST
    campaign.advertising_channel_type:
      operator: IN
      value:
        type: external_input
        key: advertising_channel_type
        split_on: ","
        format_as: SINGLE_QUOTED_LIST
        ignore_if: null

# Combined query builder
combined_query:
  type: query_builder
  query: *query
  filters: *filters
```

### Error Handling and Validation

```python
from adapt.utils import ConfigNotFoundError
from adapt.connector.service import Service

try:
    service = Service.from_config_path('invalid_config.yaml', external_input)
except ConfigNotFoundError as e:
    print(f"Configuration not found: {e}")
except Exception as e:
    print(f"Service initialization failed: {e}")
```

## 🧪 Testing and Examples

### Verify Installation

```bash
# Check package installation
pip show adapt-connector

# Test imports
python -c "import adapt.connector; print('Connector package imported successfully!')"
```

### Example: Google Ads Integration

```python
import os
os.environ['ADAPT_CONFIGS'] = '/path/to/configs'

from adapt.connector.service import Service
from adapt.connector.dispatcher import Dispatcher
from adapt.utils import Store

# Set up Google Ads authentication
auth_store = Store()
auth_store.add('client_id', 'your-client-id')
auth_store.add('client_secret', 'your-client-secret')
auth_store.add('developer_token', 'your-developer-token')
auth_store.add('refresh_token', 'your-refresh-token')

# Set up request parameters
data_store = Store()
data_store.add('customer_id', '123456789')
data_store.add('campaign_ids', '111,222,333')

# Initialize service
service = Service.from_config_path(
    'configs/connector/google/campaign.yaml',
    auth_store
)

# Load connector configuration
from adapt.utils.config_reader import YamlReader
config = YamlReader.load_from_config_location(
    module="connector",
    namespace="google",
    config_name="campaign.yaml"
)

# Execute request
response = Dispatcher.receive(service, config, data_store)
print(f"Retrieved {len(list(response))} records")
```

### Example: Facebook Ads Integration

```python
import os
os.environ['ADAPT_CONFIGS'] = '/path/to/configs'

from adapt.connector.service import Service
from adapt.utils import Store

# Set up Facebook authentication
auth_store = Store()
auth_store.add('consumer_key', 'your-app-id')
auth_store.add('consumer_secret', 'your-app-secret')
auth_store.add('refresh_token', 'your-access-token')

# Set up request parameters
data_store = Store()
data_store.add('account_id', 'act_123456789')
data_store.add('campaign_ids', '111,222,333')

# Initialize and use service
service = Service.from_config_path(
    'configs/connector/facebook/campaign.yaml',
    auth_store
)
```

## 🔗 Integration with Other Packages

### With Utils Package
```python
from adapt.utils import Store, config_reader
from adapt.connector.service import Service

# Utils provides configuration management and data storage
config = config_reader.YamlReader.load_from_config_location(
    module="connector",
    namespace="google",
    config_name="campaign.yaml"
)

auth_store = Store()
auth_store.add('client_id', 'your-client-id')

service = Service.initialize(config, auth_store)
```

### With Serializer Package
```python
from adapt.connector.dispatcher import Dispatcher
from adapt.serializer.serializer import Serializer

# Connector provides raw data
raw_data = Dispatcher.receive(client, config, external_input)

# Serializer transforms the data
serializer = Serializer.init(serializer_config)
transformed_data = serializer.serialize_records(raw_data)
```

### With Pipeline Package
```python
from adapt.pipeline.pipeline import Pipeline, Item
from adapt.connector.service import Service

# Connector as a pipeline item
pipeline = Pipeline()
pipeline.add_item(Item(
    name="data_extraction",
    processor=Service.from_config_path,
    arguments={
        "config_path": "configs/connector/google/campaign.yaml",
        "external_input": auth_store
    }
))
```

## 📖 API Reference

### Classes

#### `Authorization`
- `initialize(config, external_input=None)` - Initialize from config dict
- `from_config_path(config_path, external_input=None)` - Initialize from config file

#### `Service`
- `initialize(config, external_input)` - Initialize service from config dict
- `from_config_path(config_path, external_input)` - Initialize service from config file

#### `Dispatcher`
- `receive(client, config, external_input)` - Execute API request and return response

#### `SearchStreamToDict`
- `process(stream)` - Convert Google Ads stream response to dictionaries

### Configuration Schema

#### Authorization Configuration
```yaml
version: 1.0
kind: authorization
enabled: true
description: "Authentication description"

initializer:
  type: initializer
  client:
    type: callable|instance
    module: "module.path"
    class: "ClassName"
    method: "method_name"  # For callable type
  arguments:
    type: dict
    items:
      # Authentication parameters
```

#### Connector Configuration
```yaml
version: 1.0
kind: connector
enabled: true
description: "Connector description"

authorization:
  namespace: "api_namespace"
  config_name: "authorization.yaml"

client:
  type: from_authorizer
  method: "method_name"
  arguments:
    type: dict
    items:
      # Client initialization parameters

method: "api_method_name"
arguments:
  type: dict
  items:
    # API method parameters

post_processor:  # Optional
  type: initializer
  client:
    type: callable
    module: "processor.module"
    class: "ProcessorClass"
    method: "process_method"
  arguments:
    type: dict
    items:
      # Post-processor parameters
```

## 🤝 Contributing

This package is part of the ADaPT ecosystem. See the main project's contributing guidelines.

## 📄 License

Licensed under the Apache License 2.0. See the [LICENSE](LICENSE) file for details.

---

**Part of the ADaPT Ecosystem** - Connecting your data sources with ease. 🔌
