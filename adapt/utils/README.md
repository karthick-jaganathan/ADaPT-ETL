# ADaPT Utils - Core Utilities Package

**Configuration management, data storage, and utility functions for the ADaPT ecosystem.**

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/python-3.7+-blue.svg)](https://www.python.org/downloads/)

## 🚀 Overview

The `adapt-utils` package provides the foundational utilities and configuration management system for the ADaPT (Adaptive Data Pipeline Toolkit) ecosystem. It includes configuration readers, data storage mechanisms, type systems, and export utilities that power the entire framework.

### Utils Architecture

The utils package serves as the foundation layer for all other ADaPT components:

```
┌─────────────────────────────────────────────────────────────┐
│                        ADaPT Utils                          │
├─────────────────┬─────────────────┬─────────────────────────┤
│ Config Reader   │ Data Store      │ Type System             │
│ • YAML Parser   │ • Key-Value     │ • Type Conversion       │
│ • Path Resolver │ • State Mgmt    │ • Validation            │
│ • Validation    │ • Bulk Ops      │ • Format Handling       │
├─────────────────┼─────────────────┼─────────────────────────┤
│ Input Reader    │ Exporter        │ Environment             │
│ • File I/O      │ • CSV Export    │ • Path Management       │
│ • Format Parse  │ • Compression   │ • Variable Handling     │
│ • Stream Proc   │ • Deduplication │ • Config Discovery      │
└─────────────────┴─────────────────┴─────────────────────────┘
```

The utils package is used by all other ADaPT components:
- **Connector**: Uses config readers and type system for API setup
- **Serializer**: Leverages type conversion and data storage
- **Pipeline**: Utilizes all utils components for orchestration

## 📦 Features

- **🔧 Configuration Management**: YAML configuration reading and validation
- **💾 Data Storage**: In-memory data store for pipeline state management
- **🔄 Type System**: Flexible type conversion and validation system
- **📤 Export Utilities**: CSV export with compression and deduplication
- **📁 File I/O**: Input readers for various file formats
- **🌍 Environment Management**: Environment variable handling and path resolution

## 🛠️ Installation

### Option 1: Install from Source (Recommended)
```bash
git clone https://github.com/karthick-jaganathan/ADaPT-ETL.git
cd ADaPT-ETL/adapt/utils
pip install .
```

### Option 2: Development Installation
```bash
git clone https://github.com/karthick-jaganathan/ADaPT-ETL.git
cd ADaPT-ETL/adapt/utils
pip install -e .
```

### Option 3: As Part of Complete ADaPT Installation
```bash
git clone https://github.com/karthick-jaganathan/ADaPT-ETL.git
cd ADaPT-ETL
make install  # Installs all packages including utils
```

## 📋 Dependencies

This package automatically installs:
- `pyyaml==6.0.2` - YAML configuration parsing
- `python-dateutil>=2.8.0` - Date/time utilities

## ⚙️ Environment Variables

The utils package requires the following environment variable:

```bash
export ADAPT_CONFIGS=/path/to/your/configs
```

This variable points to the directory containing your configuration files.

## 📚 Core Components

### Configuration Management

#### YamlReader
Read and parse YAML configuration files:

```python
from adapt.utils.config_reader import YamlReader

# Read a YAML file directly
config = YamlReader.read('config.yaml')

# Load from the configured location
config = YamlReader.load_from_config_location(
    module="connector",
    namespace="google", 
    config_name="campaign.yaml"
)
```

#### Config Finder
Locate configuration files within the ADaPT configuration structure:

```python
from adapt.utils import config_finder

# Find a configuration file
config_path = config_finder(
    module="serializer",
    namespace="facebook",
    config_name="campaign.yaml"
)
# Returns: /path/to/configs/serializer/facebook/campaign.yaml
```

### Data Storage

#### Store
In-memory key-value store for pipeline state management:

```python
from adapt.utils import Store

# Create a store
store = Store()

# Add data
store.add('customer_id', '123456789')
store.add('campaign_ids', ['111', '222', '333'])

# Retrieve data
customer_id = store.get('customer_id')
campaign_ids = store.get('campaign_ids')

# Handle missing keys
value = store.get('missing_key', required=False)  # Returns None
value = store.get('missing_key', required=True)   # Returns poison pill

# Bulk operations
store.from_dict({
    'account_id': 'act_123456',
    'access_token': 'token_value'
})

# Clear all data
store.clear()
```

### Type System

#### Type Collection
Flexible type conversion and validation system:

```python
from adapt.utils import typing_collection

# Initialize types from configuration
config = {
    "type": "external_input",
    "key": "campaign_ids",
    "split_on": ",",
    "format_as": "INT_LIST"
}

result = typing_collection.init(config, store)
```

**Available Types:**

| Type             | Purpose                 | Example                                                                  |
|------------------|-------------------------|--------------------------------------------------------------------------|
| `constant`       | Static values           | `{"type": "constant", "value": "ENABLED"}`                               |
| `external_input` | User-provided data      | `{"type": "external_input", "key": "customer_id"}`                       |
| `dict`           | Dictionary construction | `{"type": "dict", "items": {...}}`                                       |
| `list`           | List construction       | `{"type": "list", "items": [...]}`                                       |
| `callable`       | Function references     | `{"type": "callable", "module": "...", "class": "...", "method": "..."}` |
| `instance`       | Object instantiation    | `{"type": "instance", "module": "...", "class": "..."}`                  |
| `query_builder`  | SQL query construction  | `{"type": "query_builder", "query": {...}, "filters": {...}}`            |

### Export Utilities

#### CSVExporter
Export data to compressed CSV files with deduplication:

```python
from adapt.utils.exporter import CSVExporter

# Configuration for export
config = {
    "export": {
        "filename": "campaign_data",
        "fields": ["campaign_id", "campaign_name", "status", "impressions"],
        "unique_on": ["campaign_id"]  # Deduplication key
    }
}

# Create exporter
exporter = CSVExporter.init(config)

# Export data
records = [
    {"campaign_id": "123", "campaign_name": "Campaign 1", "status": "ENABLED", "impressions": 1000},
    {"campaign_id": "456", "campaign_name": "Campaign 2", "status": "PAUSED", "impressions": 500}
]

file_path = exporter.export(records)
print(f"Data exported to: {file_path}")

# Or use the lazy method
file_path = CSVExporter.lazy_run(config, records)
```

**Export Features:**
- **Compression**: Automatic gzip compression
- **Deduplication**: Remove duplicate records based on specified keys
- **Timestamping**: Automatic timestamp in filename
- **Directory Management**: Automatic output directory creation
- **Field Selection**: Export only specified fields

### Input Readers

Read data from various file formats:

```python
from adapt.utils.input_reader import CSVReader

# Read CSV file
for row in CSVReader.read('data.csv'):
    print(row)  # Each row is a dictionary
```

## 🔧 Advanced Usage

### Custom Type Extensions

Extend the type system with custom types:

```python
from adapt.utils import typing_collection

class TypeCustomProcessor:
    has_store_access = True
    
    @staticmethod
    def call(_input_data, _transformation, _store):
        # Custom processing logic
        return processed_data

# Register the custom type
typing_collection.TypeCustomProcessor = TypeCustomProcessor
```

### Configuration Patterns

#### Environment-Specific Configurations

```yaml
# config.yaml
version: 1.0
environment: &env
  type: external_input
  key: environment
  required: true

database_url:
  type: case
  field: *env
  cases:
    - when: "development"
      then: "sqlite:///dev.db"
    - when: "production" 
      then: "postgresql://prod-server/db"
```

#### Conditional Processing

```yaml
# Conditional field processing
campaign_filter:
  type: filter
  items:
    campaign.status:
      operator: IN
      value:
        type: external_input
        key: campaign_status
        split_on: ","
        format_as: DOUBLE_QUOTED_LIST
        ignore_if: null  # Skip if not provided
```

### Error Handling

```python
from adapt.utils import ConfigNotFoundError

try:
    config_path = config_finder("module", "namespace", "config.yaml")
except ConfigNotFoundError as e:
    print(f"Configuration not found: {e}")
```

## 🧪 Testing and Validation

### Verify Installation

```bash
# Check package installation
pip show adapt-utils

# Test imports
python -c "import adapt.utils; print('Utils package imported successfully!')"

# Test configuration reading
python -c "
import os
os.environ['ADAPT_CONFIGS'] = '/path/to/configs'
from adapt.utils.config_reader import YamlReader
print('Configuration system working!')
"
```

### Example Usage

```python
import os
os.environ['ADAPT_CONFIGS'] = '/path/to/configs'

from adapt.utils import Store, config_reader
from adapt.utils.exporter import CSVExporter

# Create data store
store = Store()
store.add('customer_id', '123456789')
store.add('campaign_status', 'ENABLED,PAUSED')

# Read configuration
config = config_reader.YamlReader.load_from_config_location(
    module="connector",
    namespace="google",
    config_name="campaign.yaml"
)

# Export sample data
export_config = {
    "export": {
        "filename": "test_export",
        "fields": ["id", "name", "status"],
        "unique_on": ["id"]
    }
}

sample_data = [
    {"id": "1", "name": "Test Campaign", "status": "ENABLED"}
]

file_path = CSVExporter.lazy_run(export_config, sample_data)
print(f"Test export completed: {file_path}")
```

## 🔗 Integration with Other Packages

The utils package is designed to work seamlessly with other ADaPT packages:

### With Connector Package
```python
from adapt.utils import Store
from adapt.connector.service import Service

# Store provides authentication data
auth_store = Store()
auth_store.add('client_id', 'your-client-id')
auth_store.add('client_secret', 'your-client-secret')

# Service uses utils for configuration management
service = Service.from_config_path('connector_config.yaml', auth_store)
```

### With Serializer Package
```python
from adapt.utils.config_reader import YamlReader
from adapt.serializer.serializer import Serializer

# Utils provides configuration reading
config = YamlReader.read('serializer_config.yaml')

# Serializer uses the configuration
serializer = Serializer.init(config)
```

### With Pipeline Package
```python
from adapt.utils import Store, typing_collection

# Utils provides the type system for pipeline configuration
pipeline_config = {
    "type": "list",
    "items": [
        {
            "name": "data_processor",
            "type": "callable",
            "module": "my_module",
            "class": "DataProcessor",
            "method": "process"
        }
    ]
}

store = Store()
pipeline_items = typing_collection.init(pipeline_config, store)
```

## 📖 API Reference

### Classes

#### `Store`
- `add(key, value)` - Add a key-value pair
- `get(key, required=False, poison_pill="##NOT_FOUND##")` - Retrieve a value
- `clear()` - Clear all data
- `from_dict(data)` - Bulk add from dictionary

#### `YamlReader`
- `read(config_path)` - Read YAML file
- `load_from_config_location(module, namespace, config_name)` - Load from config structure

#### `CSVExporter`
- `init(config)` - Create configured exporter
- `export(records)` - Export records to file
- `lazy_run(config, records)` - One-shot export

#### `CSVReader`
- `read(feed_file)` - Read CSV file as generator

### Functions

#### `config_finder(module, namespace, config_name)`
Find configuration file path

#### `typing_collection.init(config, external_input=None)`
Initialize type from configuration

### Exceptions

#### `ConfigNotFoundError`
Raised when configuration file is not found

## 🤝 Contributing

This package is part of the ADaPT ecosystem. See the main project's contributing guidelines.

## 📄 License

Licensed under the Apache License 2.0. See the [LICENSE](LICENSE) file for details.

---

**Part of the ADaPT Ecosystem** - Core utilities powering adaptive data pipelines. 🔧
