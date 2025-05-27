---
layout: default
title: API Reference
nav_order: 5
description: "Complete API documentation for ADaPT"
permalink: /api-reference/
---

# API Reference

Complete API documentation for all ADaPT packages and their components.

## 📦 Package Overview

- **[adapt-utils](#adapt-utils-api)** - Core utilities and configuration management
- **[adapt-connector](#adapt-connector-api)** - Data source connections and API integrations
- **[adapt-serializer](#adapt-serializer-api)** - Data transformation and serialization
- **[adapt-pipeline](#adapt-pipeline-api)** - Pipeline orchestration and execution

---

## adapt-utils API

### Configuration Management

#### `adapt.utils.config_reader.YamlReader`

YAML configuration file reader and parser.

**Methods:**

```python
@staticmethod
def read(file_path: str) -> dict
```
Read and parse a YAML file.
- **Parameters**: `file_path` (str) - Path to the YAML file
- **Returns**: dict - Parsed YAML content
- **Raises**: `FileNotFoundError`, `yaml.YAMLError`

```python
@staticmethod
def load_from_config_location(module: str, namespace: str, config_name: str) -> dict
```
Load configuration from the standard ADaPT configuration structure.
- **Parameters**: 
  - `module` (str) - Module name (connector, serializer, authorization, pipeline)
  - `namespace` (str) - Service namespace
  - `config_name` (str) - Configuration file name
- **Returns**: dict - Parsed configuration
- **Environment**: Requires `ADAPT_CONFIGS` environment variable

#### `adapt.utils.config_finder`

```python
def config_finder(module: str, namespace: str, config_name: str) -> str
```
Locate configuration files within the ADaPT configuration structure.
- **Parameters**: 
  - `module` (str) - Module name
  - `namespace` (str) - Service namespace  
  - `config_name` (str) - Configuration file name
- **Returns**: str - Full path to configuration file

### Data Storage

#### `adapt.utils.Store`

In-memory key-value store for pipeline state management.

**Constructor:**
```python
def __init__(self)
```

**Methods:**

```python
def add(self, key: str, value: Any) -> None
```
Add a key-value pair to the store.
- **Parameters**: 
  - `key` (str) - Storage key
  - `value` (Any) - Value to store

```python
def get(self, key: str, required: bool = True) -> Any
```
Retrieve a value from the store.
- **Parameters**: 
  - `key` (str) - Storage key
  - `required` (bool) - Whether the key is required
- **Returns**: Any - Stored value or None/poison pill if not found

```python
def from_dict(self, data: dict) -> None
```
Bulk load data from a dictionary.
- **Parameters**: `data` (dict) - Dictionary to load

```python
def clear(self) -> None
```
Clear all stored data.

### Type System

#### `adapt.utils.typing_collection`

```python
def init(config: dict, store: Store) -> Any
```
Initialize and process type configurations.
- **Parameters**: 
  - `config` (dict) - Type configuration
  - `store` (Store) - Data store instance
- **Returns**: Any - Processed value based on type configuration

**Supported Types:**
- `constant` - Static values
- `external_input` - User-provided data from store
- `dict` - Dictionary construction
- `list` - List construction  
- `callable` - Function/method references
- `instance` - Object instantiation
- `query_builder` - SQL query construction

### Export Utilities

#### `adapt.utils.exporter.CSVExporter`

CSV export with compression and deduplication capabilities.

**Methods:**

```python
@classmethod
def init(cls, config: dict) -> 'CSVExporter'
```
Initialize exporter from configuration.
- **Parameters**: `config` (dict) - Export configuration
- **Returns**: CSVExporter instance

```python
def export(self, records: List[dict]) -> str
```
Export records to CSV file.
- **Parameters**: `records` (List[dict]) - Data records to export
- **Returns**: str - Path to exported file

```python
@classmethod
def lazy_run(cls, config: dict, records: List[dict]) -> str
```
One-shot export method.
- **Parameters**: 
  - `config` (dict) - Export configuration
  - `records` (List[dict]) - Data records to export
- **Returns**: str - Path to exported file

---

## adapt-connector API

### Authorization

#### `adapt.connector.authorization.Authorizer`

Handles API authentication and authorization.

**Constructor:**
```python
def __init__(self, auth_type: str, credentials: dict)
```

**Methods:**

```python
def get_client(self, service_name: str) -> Any
```
Get authenticated API client.
- **Parameters**: `service_name` (str) - Name of the service
- **Returns**: Any - Authenticated client instance

### Service Integration

#### `adapt.connector.service.Service`

Main service integration class for API connections.

**Methods:**

```python
@classmethod
def initialize(cls, config: dict, external_input: Store) -> Any
```
Initialize service from configuration.
- **Parameters**: 
  - `config` (dict) - Service configuration
  - `external_input` (Store) - External input data
- **Returns**: Any - Service response/data

### Request Dispatching

#### `adapt.connector.dispatcher.Dispatcher`

Handles API request dispatching and response management.

**Constructor:**
```python
def __init__(self, client: Any, requests: List[dict])
```

**Methods:**

```python
def dispatch(self) -> Generator[Any, None, None]
```
Dispatch requests and yield responses.
- **Returns**: Generator - API responses

### Post-Processing

#### `adapt.connector.post_processor.SearchStreamToDict`

Post-processor for Google Ads API streaming responses.

**Methods:**

```python
@staticmethod
def process(stream: Any) -> Generator[dict, None, None]
```
Convert protobuf stream to dictionaries.
- **Parameters**: `stream` (Any) - API response stream
- **Returns**: Generator[dict] - Converted dictionaries

---

## adapt-serializer API

### Data Transformation

#### `adapt.serializer.serializer.Serializer`

Main data transformation and serialization engine.

**Methods:**

```python
@classmethod
def init(cls, config: dict) -> 'Serializer'
```
Initialize serializer from configuration.
- **Parameters**: `config` (dict) - Serializer configuration
- **Returns**: Serializer instance

```python
def serialize_records(self, records: Iterable[dict]) -> Generator[dict, None, None]
```
Transform records according to configuration.
- **Parameters**: `records` (Iterable[dict]) - Input records
- **Returns**: Generator[dict] - Transformed records

```python
def serialize_record(self, record: dict) -> dict
```
Transform a single record.
- **Parameters**: `record` (dict) - Input record
- **Returns**: dict - Transformed record

### Type System

#### `adapt.serializer.serializer_typing`

Type conversion and validation utilities for serialization.

**Functions:**

```python
def convert_type(value: Any, type_config: dict) -> Any
```
Convert value according to type configuration.
- **Parameters**: 
  - `value` (Any) - Value to convert
  - `type_config` (dict) - Type conversion configuration
- **Returns**: Any - Converted value

**Supported Transform Types:**
- `string` - String conversion
- `integer` - Integer conversion
- `float` - Float conversion with optional precision
- `boolean` - Boolean conversion
- `enum` - Enumeration mapping
- `case` - Conditional transformations
- `formula` - Mathematical expressions

---

## adapt-pipeline API

### Pipeline Orchestration

#### `adapt.pipeline.pipeline.Pipeline`

Main pipeline orchestration and execution engine.

**Methods:**

```python
def execute(self, config: dict, external_input: Store) -> Any
```
Execute pipeline according to configuration.
- **Parameters**: 
  - `config` (dict) - Pipeline configuration
  - `external_input` (Store) - External input data
- **Returns**: Any - Pipeline execution result

### CLI Interface

#### `adapt.pipeline.data_ingest_cli`

Command-line interface for pipeline execution.

**Functions:**

```python
def main() -> None
```
Main CLI entry point. Handles command-line arguments and executes pipelines.

**CLI Arguments:**
- `--namespace` - Service namespace
- `--pipeline-config` - Pipeline configuration file
- `--data-ingestion-config` - Data ingestion configuration
- `--auth-data` - Authentication parameters (key=value format)
- `--external-input` - External input parameters (key=value format)
- `--output-dir` - Override output directory
- `--verbose` - Enable verbose logging

---

## Configuration Schemas

### Pipeline Configuration Schema

```yaml
version: str                    # Configuration version
kind: "pipeline"               # Configuration type
description: str               # Optional description

pipeline:
  type: "list"                 # Pipeline type
  items:                       # Pipeline items
    - name: str                # Item name
      client:                  # Client configuration
        type: str              # Client type (callable, instance)
        module: str            # Python module
        class: str             # Python class
        method: str            # Method name (for callable)
      arguments:               # Method/constructor arguments
        type: "dict"
        items: dict            # Argument specifications
```

### Connector Configuration Schema

```yaml
version: str                   # Configuration version
kind: "connector"             # Configuration type
description: str              # Optional description

authorization:                # Authorization configuration
  namespace: str              # Authorization namespace
  config_name: str            # Authorization config file

client:                       # Client configuration
  type: str                   # Client type
  method: str                 # Method name
  arguments: dict             # Client arguments

dispatcher:                   # Request dispatcher configuration
  type: str                   # Dispatcher type
  module: str                 # Python module
  class: str                  # Python class
  arguments: dict             # Constructor arguments

post_processor:               # Optional post-processor
  type: str                   # Processor type
  module: str                 # Python module
  class: str                  # Python class
  arguments: dict             # Constructor arguments
```

### Serializer Configuration Schema

```yaml
version: str                  # Configuration version (0.1)
kind: "serializer"           # Configuration type

inline:                      # Field transformations
  - name: str                # Output field name
    from: str                # Source field name (optional)
    object: str              # Source object path (optional)
    transform:               # Transformation configuration
      type: str              # Transform type
      # Additional type-specific parameters

derived:                     # Computed fields
  - name: str                # Output field name
    transform: dict          # Transformation configuration

constants:                   # Static fields
  - name: str                # Output field name
    value: Any               # Static value
    transform: dict          # Optional transformation

export:                      # Export configuration
  filename: str              # Output filename
  fields: List[str]          # Fields to export
  unique_on: List[str]       # Deduplication keys
```

### Authorization Configuration Schema

```yaml
version: str                 # Configuration version
kind: "authorization"        # Configuration type
description: str             # Optional description

authorizer:                  # Authorizer configuration
  type: str                  # Authorizer type
  module: str                # Python module
  class: str                 # Python class
  arguments:                 # Constructor arguments
    type: "dict"
    items:
      auth_type: dict        # Authentication type
      credentials: dict      # Credential specifications
```

---

## Error Handling

### Common Exceptions

#### `ConfigurationError`
Raised when configuration is invalid or missing.

#### `AuthenticationError`
Raised when API authentication fails.

#### `TransformationError`
Raised when data transformation fails.

#### `ExportError`
Raised when data export fails.

### Error Handling Best Practices

1. **Configuration Validation**: Always validate configurations before execution
2. **Graceful Degradation**: Handle missing optional fields gracefully
3. **Logging**: Use appropriate logging levels for debugging
4. **Resource Cleanup**: Ensure proper cleanup of resources and connections

---

## Environment Variables

### Required Variables

- `ADAPT_CONFIGS` - Path to configuration directory

### Optional Variables

- `ADAPT_OUTPUT_DIR` - Output directory for exported data (default: `/tmp`)

---

## Examples

For practical usage examples, see the [Examples]({{ site.baseurl }}/examples) section.

For package-specific documentation, see the [Packages]({{ site.baseurl }}/packages/) section. 