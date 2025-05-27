# ADaPT - Adaptive Data Pipeline Toolkit

**A Python-based, YAML-configured data pipeline framework for extracting, transforming, and exporting data from various sources to multiple destinations.**

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/python-3.7+-blue.svg)](https://www.python.org/downloads/)

## 🚀 Overview

ADaPT (Adaptive Data Pipeline Toolkit) is a modular, configuration-driven ETL framework designed to simplify data extraction, transformation, and loading processes. Built with flexibility and extensibility in mind, ADaPT allows you to create complex data pipelines using simple YAML configurations.

### Key Features

- **🔧 Configuration-Driven**: Define entire pipelines using YAML configurations
- **🔌 Modular Architecture**: Four independent packages that work together seamlessly
- **🌐 Multi-Source Support**: Extensible connector architecture for APIs, databases, and file systems
- **🔄 Flexible Transformations**: Powerful serialization engine with conditional logic and data normalization
- **📦 Multiple Installation Options**: Install complete toolkit or individual components
- **🐳 Docker Support**: Containerized deployment with Docker and Docker Compose
- **🛠️ Developer Friendly**: Comprehensive CLI tools and development utilities
- **🔄 Cross-Platform**: Compatible with different protobuf versions (4.x and 5.x+)

## 📋 Table of Contents

- [Architecture](#-architecture)
- [Installation](#-installation)
- [Quick Start](#-quick-start)
- [Usage Examples](#-usage-examples)
- [Configuration](#-configuration)
- [Docker Deployment](#-docker-deployment)
- [Contributing](#-contributing)
- [Support](#-support)

## 🏗️ Architecture

ADaPT consists of four core packages that form a complete data pipeline ecosystem:

```
┌─────────────────┐    ┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   adapt-utils   │    │ adapt-connector │    │adapt-serializer  │    │ adapt-pipeline  │
│                 │    │                 │    │                  │    │                 │
│ • Configuration │    │ • Authorization │    │ • Data Transform │    │ • Orchestration │
│ • Data Store    │    │ • API Clients   │    │ • Field Mapping  │    │ • CLI Interface │
│ • Type System   │    │ • Dispatchers   │    │ • Normalization  │    │ • Workflow Mgmt │
│ • File I/O      │    │ • Post Process  │    │                  │    │                 │
└─────────────────┘    └─────────────────┘    └──────────────────┘    └─────────────────┘
        │                        │                        │                        │
        └────────────────────────┼────────────────────────┼────────────────────────┘
                                 │                        │
                    ┌─────────────────────────────────────────────────────┐
                    │                ADaPT Pipeline                       │
                    │                                                     │
                    │         Extract → Transform → Load → Export         │
                    └─────────────────────────────────────────────────────┘
```

### Package Responsibilities

| Package              | Purpose                                      | Key Components                                                 |
|----------------------|----------------------------------------------|----------------------------------------------------------------|
| **adapt-utils**      | Core utilities and configuration management  | Config readers, data store, type system, exporters             |
| **adapt-connector**  | Data source connections and API integrations | Authorization, service clients, dispatchers, post-processors   |
| **adapt-serializer** | Data transformation and serialization        | Field mapping, data normalization, conditional transformations |
| **adapt-pipeline**   | Pipeline orchestration and execution         | CLI interface, workflow management, pipeline items             |

## 🛠️ Installation

### Quick Installation

```bash
# Clone and install
git clone https://github.com/karthick-jaganathan/ADaPT-ETL.git
cd ADaPT-ETL
make install

# Verify installation
adapt_pipeline --help
```

### Installation Options

- **Complete Installation**: `make install` - Installs all packages
- **Development Mode**: `make install MODE=dev` - Editable installation for development
- **Individual Packages**: Install only specific components you need
- **Docker Deployment**: Use Docker Compose for containerized setup

📖 **For detailed installation instructions, troubleshooting, and advanced options, see [INSTALLATION.md](INSTALLATION.md)**

## 🚀 Quick Start

### 1. Set Environment Variables

```bash
export ADAPT_CONFIGS="$(pwd)/configs"
export ADAPT_OUTPUT_DIR="/path/to/output"  # Optional, defaults to /tmp
```

### 2. Run Your First Pipeline

#### Basic Pipeline Example

```bash
adapt_pipeline --namespace your_namespace \
  --pipeline-config data_ingestion.yaml \
  --data-ingestion-config your_config.yaml \
  --auth-data api_key="your-api-key" \
  --auth-data api_secret="your-api-secret" \
  --external-input resource_id="your-resource-id"
```

#### API Integration Example

```bash
adapt_pipeline --namespace api_service \
  --pipeline-config data_extraction.yaml \
  --data-ingestion-config endpoint_config.yaml \
  --auth-data access_token="your-access-token" \
  --auth-data base_url="https://api.example.com" \
  --external-input resource_ids="123,456,789" \
  --external-input date_range="2024-01-01,2024-01-31"
```

## 📚 Usage Examples

### Python API Usage

```python
from adapt.utils import Store, config_reader
from adapt.connector.service import Service
from adapt.serializer.serializer import Serializer
from adapt.utils.exporter import CSVExporter

# Set up data store
external_input = Store()
external_input.add('resource_id', '123456789')
external_input.add('entity_ids', '111,222,333')

# Load configuration
config = config_reader.YamlReader.load_from_config_location(
    module="connector",
    namespace="your_namespace",
    config_name="your_config.yaml"
)

# Initialize service
service = Service.initialize(config, external_input)

# Process data through pipeline
# ... (additional pipeline steps)
```

### Custom Serialization

First, define your transformation configuration in YAML:

```yaml
# configs/serializer/your_service/data_transform.yaml
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
  
  - name: status_label
    transform:
      type: case
      cases:
        - when:
            field: status
            equals: "ACTIVE"
          then: "Active"
        - when:
            field: status
            equals: "INACTIVE"
          then: "Inactive"
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
  
  - name: category_code
    from: category
    transform:
      type: enum
      mapping:
        "SALES": "S"
        "MARKETING": "M"
        "SUPPORT": "SUP"
        "DEVELOPMENT": "DEV"
      default: "OTHER"
```

Then load and use the configuration:

```python
from adapt.serializer.serializer import Serializer
from adapt.utils.config_reader import YamlReader

# Load configuration from YAML file
config = YamlReader.load_from_config_location(
    module="serializer",
    namespace="your_service",
    config_name="data_transform.yaml"
)

# Create serializer and process data
serializer = Serializer.init(config)

# Sample raw data with enum values
raw_data = [
    {
        "id": "123",
        "name": "Sample Entity",
        "status": "ACTIVE",
        "priority": 3,
        "category": "SALES"
    }
]

transformed_data = list(serializer.serialize_records(raw_data))
# Result: [{"entity_id": 123, "entity_name": "Sample Entity", "status_label": "Active", 
#          "priority_level": "High", "category_code": "S"}]
```

## ⚙️ Configuration

ADaPT uses YAML configurations to define pipelines, connectors, serializers, and authorization. The configuration structure follows a modular approach:

```
configs/
├── pipeline/
│   └── data_ingestion.yaml      # Pipeline orchestration
├── connector/
│   ├── your_service/
│   │   └── data_config.yaml     # Service connector config
│   └── another_api/
│       └── endpoint_config.yaml # Another API connector config
├── serializer/
│   ├── your_service/
│   │   └── data_transform.yaml  # Service data transformation
│   └── another_api/
│       └── data_mapping.yaml    # Another API data transformation
└── authorization/
    ├── your_service/
    │   └── authorization.yaml   # Service authentication
    └── another_api/
        └── authorization.yaml   # Another API authentication
```

### Configuration Examples

#### Pipeline Configuration
```yaml
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
      # ... additional configuration
```

#### Connector Configuration
```yaml
version: 1.0
kind: connector
description: "Generic API connector"

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
        value: DataService
```

#### Serializer Configuration

if you want to transform data, you can define a serializer configuration like this:

To transform following data, you can define a serializer configuration like this:

```json
{
    "entity": {
        "id": 123,
        "name": "Sample Entity"
    },
    "metrics": {
        "value": 456
    }
}
```

```yaml
version: 0.1
kind: serializer

inline:
  - name: entity_id
    object: entity
    from: id
    transform:
      type: integer
  - name: entity_name
    object: entity
    from: name
    transform:
      type: string
  - name: metric_value
    object: metrics
    from: value
    transform:
      type: integer
```

For more details, you can refer to the [serializer documentation](adapt/serializer/README.md). and examples in the `adapt/serializer/examples/` directory.

## 🐳 Docker Deployment

ADaPT provides Docker support for containerized deployments. The Docker setup includes all necessary dependencies and packages pre-installed.

### Using Docker Compose (Recommended)

```bash
# Build the container (includes all ADaPT packages)
docker-compose build

# Start container in background
docker-compose up -d

# Execute pipeline in running container
docker-compose exec adapt-etl adapt_pipeline --namespace your_service \
  --pipeline-config data_ingestion.yaml \
  --data-ingestion-config data_config.yaml \
  --auth-data api_key="your-api-key" \
  # ... additional parameters

# Alternative: Run pipeline in new container (no need to start first)
docker-compose run --rm adapt-etl adapt_pipeline --namespace your_service \
  --pipeline-config data_ingestion.yaml \
  --data-ingestion-config data_config.yaml \
  --auth-data api_key="your-api-key" \
  # ... additional parameters

# Stop containers
docker-compose stop

# Remove containers and networks
docker-compose down
```

### Using Docker Directly

```bash
# Build image
docker build -t adapt-etl .

# Run pipeline
docker run -it --rm \
  -v $(pwd)/configs:/configs:ro \
  -v /data/adapt_etl:/data/adapt_etl \
  adapt-etl adapt_pipeline --namespace your_service \
  --pipeline-config data_ingestion.yaml \
  --data-ingestion-config data_config.yaml \
  # ... additional parameters
```



## 🤝 Contributing

We welcome contributions! Here's how to get started:

### Development Setup

1. **Fork the repository** and clone your fork
2. **Set up development environment** - See [INSTALLATION.md](INSTALLATION.md#development-setup) for detailed instructions
3. **Install in development mode**: `make install MODE=dev`
4. **Make your changes** and test thoroughly
5. **Submit a pull request** with a clear description

### Development Guidelines

- Follow existing code style and patterns
- Add tests for new functionality when applicable
- Update documentation for any API changes
- Ensure all packages install and import correctly

## 📄 License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.

## 🆘 Support

- **Installation Help**: See [INSTALLATION.md](INSTALLATION.md) for detailed installation instructions and troubleshooting
- **Package Documentation**: Check individual package READMEs in `adapt/*/README.md`
- **Issues**: Report bugs and request features on [GitHub Issues](https://github.com/karthick-jaganathan/ADaPT-ETL/issues)
- **Examples**: See the `adapt/serializer/examples/` directory for usage examples

---

**ADaPT** - Making data pipelines adaptive, configurable, and powerful. 🚀