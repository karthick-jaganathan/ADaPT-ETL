# ADaPT Serializer - Data Transformation Package

**Flexible data serialization, transformation, and normalization for the ADaPT ecosystem.**

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/python-3.7+-blue.svg)](https://www.python.org/downloads/)

## 🚀 Overview

The `adapt-serializer` package provides powerful data transformation and serialization capabilities for the ADaPT (Adaptive Data Pipeline Toolkit) ecosystem. It enables you to transform raw API responses into structured, normalized data through flexible YAML configurations with support for field mapping, conditional logic, data type conversion, and nested object handling.

## 📦 Features

- **🔄 Field Mapping**: Map source fields to target fields with custom naming
- **🎯 Data Type Conversion**: Convert between different data types (string, integer, float, boolean)
- **🔀 Conditional Logic**: Apply transformations based on field values and conditions
- **📊 Data Normalization**: Flatten nested objects and normalize complex data structures
- **🔗 Array Processing**: Handle nested arrays with inline and extended processing modes
- **📝 Template System**: Use constants and derived fields for data enrichment
- **🚫 Selective Filtering**: Include/exclude fields based on conditions
- **⚙️ Configuration-Driven**: Define all transformations through YAML configurations

## 🛠️ Installation

### Option 1: Install from Source (Recommended)
```bash
git clone https://github.com/karthick-jaganathan/ADaPT-ETL.git
cd ADaPT-ETL/adapt/serializer
pip install .
```

This will automatically install all required dependencies:
- `adapt-utils` (Core utilities)

### Option 2: Development Installation
```bash
git clone https://github.com/karthick-jaganathan/ADaPT-ETL.git
cd ADaPT-ETL/adapt/serializer
pip install -e .
```

### Option 3: As Part of Complete ADaPT Installation
```bash
git clone https://github.com/karthick-jaganathan/ADaPT-ETL.git
cd ADaPT-ETL
make install  # Installs all packages including serializer
```

**Note**: If automatic dependency installation fails, install dependencies manually:
```bash
cd ../utils && pip install .
cd ../serializer && pip install .
```

## 📋 Dependencies

This package depends on:
- **adapt-utils**: Core utilities (automatically installed)

## ⚙️ Environment Variables

The serializer package requires:

```bash
export ADAPT_CONFIGS=/path/to/your/configs
```

## 📚 Core Components

### Serializer Class

The main `Serializer` class handles data transformation based on configuration:

```python
from adapt.serializer.serializer import Serializer

# Initialize from configuration
config = {
    "inline": [
        {
            "name": "campaign_id",
            "from": "id",
            "transform": {"type": "integer"}
        }
    ]
}

serializer = Serializer.init(config)

# Transform data
raw_data = [{"id": "123", "name": "Campaign 1"}]
transformed_data = list(serializer.serialize_records(raw_data))
```

## 🔧 Configuration Structure

### Basic Configuration Schema

```yaml
version: 0.1
kind: serializer

# Inline field transformations
inline:
  - name: output_field_name
    from: source_field_name
    transform:
      type: transformation_type
      # Additional transformation parameters

# Derived fields (computed from other fields)
derived:
  - name: computed_field_name
    transform:
      type: transformation_type
      # Transformation logic

# Constants (static values)
constants:
  - name: constant_field_name
    value: static_value
    transform:
      type: transformation_type
```

### Transformation Types

#### 1. Basic Data Types

```yaml
# String transformation
- name: campaign_name
  from: name
  transform:
    type: string

# Integer transformation
- name: campaign_id
  from: id
  transform:
    type: integer

# Float transformation with precision
- name: cost_per_click
  from: cpc
  transform:
    type: float
    precision: 2

# Boolean transformation
- name: is_active
  from: status
  transform:
    type: boolean

# Enum transformation
- name: priority_label
  from: priority_code
  transform:
    type: enum
    mappings:
      1: "Low"
      2: "Medium"
      3: "High"
    default: "Unknown"
```

#### 2. Conditional Transformations

```yaml
# Case-based transformation
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

# Conditional with multiple criteria
- name: performance_tier
  transform:
    type: case
    cases:
      - when:
          field: impressions
          greater_than: 10000
        then: "High Volume"
      - when:
          field: cost_micros
          greater_than: 1000000
        then: "High Spend"
    default: "Standard"
```

#### 3. Nested Object Access

```yaml
# Access nested object properties
- name: budget_amount
  from: amount_micros
  object: budget
  transform:
    type: integer

# Multiple level nesting
- name: targeting_location
  from: location.name
  object: targeting.geo_targeting.locations.0
  transform:
    type: string
```

#### 4. Currency and Date Transformations

```yaml
# Currency conversion (micros to dollars)
- name: budget_amount
  from: amount_micros
  object: budget
  transform:
    type: currency
    multiplier: 0.000001
    rounding: 2

# Date format conversion
- name: formatted_date
  from: date_string
  transform:
    type: date
    format:
      input: "%Y-%m-%d"
      output: "%m/%d/%Y"
```

#### 5. Conditional Field Inclusion

```yaml
# Include field only if condition is met
- name: end_date
  from: end_date
  transform:
    type: string
  ignore:
    when:
      field: end_date
      equals: null
```

## 🎯 Advanced Usage Examples

### Example 1: E-commerce Campaign Data

```yaml
version: 0.1
kind: serializer

inline:
  # Basic field mapping
  - name: campaign_id
    from: id
    transform:
      type: integer

  - name: campaign_name
    from: name
    transform:
      type: string

  # Status transformation with labels
  - name: status
    from: status
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
      default: "Unknown"

  # Budget conversion (micros to currency)
  - name: daily_budget
    object: budget
    from: amount_micros
    transform:
      type: float
      # Custom transformation to convert micros to dollars
      divide_by: 1000000

  # Performance metrics
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

  - name: cost
    object: metrics
    from: cost_micros
    transform:
      type: float
      divide_by: 1000000

# Derived fields (using ratio transformation)
derived:
  - name: click_through_rate
    transform:
      type: ratio
      numerator: "clicks"
      denominator: "impressions"
      precision: 4
      default: 0.0

  - name: cost_per_click
    transform:
      type: ratio
      numerator: "cost"
      denominator: "clicks"
      precision: 2
      default: 0.0

# Constants
constants:
  - name: data_source
    value: "api_source"
    transform:
      type: string

  - name: extraction_date
    value: "2024-01-15"
    transform:
      type: string
```

### Example 2: Social Media Campaign Data

```yaml
version: 0.1
kind: serializer

inline:
  - name: campaign_id
    from: id
    transform:
      type: string

  - name: campaign_name
    from: name
    transform:
      type: string

  # Objective mapping
  - name: objective
    from: objective
    transform:
      type: case
      cases:
        - when:
            field: objective
            equals: "LINK_CLICKS"
          then: "Traffic"
        - when:
            field: objective
            equals: "CONVERSIONS"
          then: "Conversions"
        - when:
            field: objective
            equals: "REACH"
          then: "Brand Awareness"
      default: "Other"

  # Bid strategy transformation
  - name: bidding_strategy
    from: bid_strategy
    transform:
      type: case
      cases:
        - when:
            field: bid_strategy
            equals: "LOWEST_COST_WITHOUT_CAP"
          then: "Automatic"
        - when:
            field: bid_strategy
            equals: "COST_CAP"
          then: "Cost Cap"
      default: "Manual"

  # Nested targeting information
  - name: age_min
    from: age_min
    object: targeting.age_range
    transform:
      type: integer

  - name: age_max
    from: age_max
    object: targeting.age_range
    transform:
      type: integer

  # Geographic targeting
  - name: location_name
    from: name
    object: targeting.location
    transform:
      type: string

  - name: location_type
    from: type
    object: targeting.location
    transform:
      type: enum
      mappings:
        "CITY": "City"
        "REGION": "State/Region"
        "COUNTRY": "Country"
      default: "Unknown"

# Export configuration
export:
  filename: "api_data_export"
  fields:
    - campaign_id
    - campaign_name
    - objective
    - bidding_strategy
    - age_min
    - age_max
    - location_name
    - location_type
  unique_on:
    - campaign_id
```

### Example 3: Data Normalization

```python
# Example with dictionary normalization
from adapt.serializer.serializer import Serializer

config = {
    "inline": [
        {
            "name": "campaign_id",
            "from": "campaign.id",
            "transform": {"type": "integer"}
        },
        {
            "name": "campaign_name", 
            "from": "campaign.name",
            "transform": {"type": "string"}
        }
    ]
}

# Enable dictionary normalization
serializer = Serializer.init(config, dict_normalize=True)

# Raw nested data
raw_data = [
    {
        "campaign": {
            "id": "123",
            "name": "Summer Sale"
        },
        "metrics": {
            "impressions": 1000,
            "clicks": 50
        }
    }
]

# Transform with normalization
transformed_data = list(serializer.serialize_records(raw_data))
# Result: [{"campaign_id": 123, "campaign_name": "Summer Sale"}]
```

## 🧪 Testing and Examples

### Verify Installation

```bash
# Check package installation
pip show adapt-serializer

# Test imports
python -c "import adapt.serializer; print('Serializer package imported successfully!')"
```

### Running Examples

The package includes comprehensive example scripts that demonstrate real-world usage scenarios. These examples show how to transform complex API response data using YAML configurations.

#### Prerequisites

Before running examples, ensure you have the serializer package installed:

```bash
# From the ADaPT-ETL root directory
cd adapt/serializer
pip install .
```

#### Available Examples

The examples directory contains:
- `json_pipeline.py` - Demonstrates campaign data transformation with complex field mappings
- `dict_normalization.py` - Shows nested object flattening and array processing
- `configs/` - YAML configuration files for each example

#### Example 1: JSON Pipeline Transformation

This example transforms social media campaign data with complex field mappings, enum transformations, and conditional logic.

```bash
# Navigate to serializer directory
cd ADaPT-ETL/adapt/serializer

# Run the JSON pipeline example
python examples/json_pipeline.py
```

**What it demonstrates:**
- Campaign data transformation from API response format
- Status code mapping using enums (`ACTIVE` → `a`, `PAUSED` → `p`)
- Budget handling with conditional logic (daily vs lifetime budgets)
- Date parsing and formatting
- Nested object field extraction (`promoted_object.custom_conversion_id`)
- Currency conversion with multipliers

**Sample output:**
```
----------------------------------------------------------------------------------------------------
{'campaignid': 23861273961600732, 'adref': 23861274251890732, 'adgroup_name': 'campaign 01', 'status_code': 'a', 'budget': 80300.0, 'budget_type': 'daily', 'pricing_type': 'Cost Per Result Goal', 'remaining_budget': 80300.0, 'spend_limit': None, 'created_date': '2023-10-13', 'start_date': '2023-10-13', 'updated_date': '2023-10-13', 'end_date': None, 'optimization_goal': 'Conversions', 'custom_conversion_id': '719479913527844', 'custom_event_type': 'OTHER', 'custom_event_str': None, 'bid_amount': 23000.0, 'target_roas_percent': None, 'target_cpa': 23000.0, 'bid_cap': None, 'optimizing_event': '719479913527844'}
----------------------------------------------------------------------------------------------------
{'campaignid': 23857528383050732, 'adref': 23857924806060732, 'adgroup_name': 'campaign 02', 'status_code': 'p', 'budget': 200000.0, 'budget_type': 'daily', 'pricing_type': 'Highest Volume', 'remaining_budget': 200000.0, 'spend_limit': None, 'created_date': '2023-09-12', 'start_date': '2023-09-12', 'updated_date': '2023-09-12', 'end_date': None, 'optimization_goal': 'Conversions', 'custom_conversion_id': None, 'custom_event_type': 'ADD_TO_CART', 'custom_event_str': None, 'bid_amount': None, 'target_roas_percent': None, 'target_cpa': None, 'bid_cap': None, 'optimizing_event': 'offsite_conversion.fb_pixel_add_to_cart'}
----------------------------------------------------------------------------------------------------
{'campaignid': 23861273961600732, 'adref': 23861274251890732, 'adgroup_name': 'campaign 03', 'status_code': 'a', 'budget': 80300.0, 'budget_type': 'daily', 'pricing_type': 'Cost Per Result Goal', 'remaining_budget': 80300.0, 'spend_limit': None, 'created_date': '2023-10-13', 'start_date': '2023-10-13', 'updated_date': '2023-10-13', 'end_date': None, 'optimization_goal': 'Conversions', 'custom_conversion_id': None, 'custom_event_type': 'OTHER', 'custom_event_str': 'app_install', 'bid_amount': 23000.0, 'target_roas_percent': None, 'target_cpa': 23000.0, 'bid_cap': None, 'optimizing_event': None}
```

#### Example 2: Dictionary Normalization

This example shows how to handle deeply nested data structures and flatten them into normalized records.

```bash
# Run without normalization (default)
python examples/dict_normalization.py

# Run with dictionary normalization enabled
python examples/dict_normalization.py --dict-normalize
```

**What it demonstrates:**
- Nested campaign/ad set/ad hierarchy processing
- Array handling for multiple ad sets and ads
- Conditional field inclusion based on data availability
- Different behavior with and without dictionary normalization
- Complex nested object access patterns

**Key differences:**
- **Without `--dict-normalize`**: Processes nested objects as-is, creating separate records for each level (campaigns, ad sets, ads)
- **With `--dict-normalize`**: Flattens nested structures before processing, creating uniform records with all possible fields

**Sample output without `--dict-normalize`:**
```
----------------------------------------------------------------------------------------------------
{'campaignid': 22345865434546, 'campaign_name': 'Campaign 01', 'status_code': 'a', 'start_date': '2023-01-23', 'end_date': '2024-01-23', 'budget': None, 'budget_level': 'AdGroup', 'budget_typeid': 1}
----------------------------------------------------------------------------------------------------
{'adset_id': 347563424354654, 'adset_name': 'Campaign 01 :: adSet 01', 'adset_status_code': 'a', 'adset_start_date': '2023-01-23', 'adset_end_date': '2024-01-23', 'adset_budget': 100.0, 'optimization_goal': 'LINK_CLICKS', 'optimization_event': 'ADD_TO_CART', 'campaignid': 22345865434546, 'campaign_name': 'Campaign 01', 'status_code': 'a', 'start_date': '2023-01-23', 'end_date': '2024-01-23', 'budget': None, 'budget_level': 'AdGroup', 'budget_typeid': 1}
----------------------------------------------------------------------------------------------------
{'creative_id': 123456789, 'creative_name': 'Campaign 01 :: adSet 01 :: ad 01', 'creative_status_code': 'a', 'adset_id': 347563424354654, 'adset_name': 'Campaign 01 :: adSet 01', 'adset_status_code': 'a', 'adset_start_date': '2023-01-23', 'adset_end_date': '2024-01-23', 'adset_budget': 100.0, 'optimization_goal': 'LINK_CLICKS', 'optimization_event': 'ADD_TO_CART', 'campaignid': 22345865434546, 'campaign_name': 'Campaign 01', 'status_code': 'a', 'start_date': '2023-01-23', 'end_date': '2024-01-23', 'budget': None, 'budget_level': 'AdGroup', 'budget_typeid': 1}
```

**Sample output with `--dict-normalize`:**
```
----------------------------------------------------------------------------------------------------
{'budget_level': 'AdGroup', 'end_date': '2024-01-23', 'adset_end_date': None, 'creative_name': None, 'optimization_event': None, 'adset_start_date': None, 'optimization_goal': None, 'start_date': '2023-01-23', 'campaignid': 22345865434546, 'campaign_name': 'Campaign 01', 'budget': None, 'adset_status_code': None, 'budget_typeid': 1, 'creative_id': None, 'status_code': 'a', 'adset_id': None, 'creative_status_code': None, 'adset_name': None, 'adset_budget': None}
----------------------------------------------------------------------------------------------------
{'budget_level': 'Campaign', 'end_date': '2024-01-23', 'adset_end_date': None, 'creative_name': None, 'optimization_event': None, 'adset_start_date': None, 'optimization_goal': None, 'start_date': '2023-01-23', 'campaignid': 12345678765432, 'campaign_name': 'Campaign 02', 'budget': 100.0, 'adset_status_code': None, 'budget_typeid': 1, 'creative_id': None, 'status_code': 'a', 'adset_id': None, 'creative_status_code': None, 'adset_name': None, 'adset_budget': None}
```

**Notice how:**
- **Without normalization:** Creates hierarchical records (campaign → ad set → ad) with inherited fields
- **With normalization:** Creates flattened records with consistent field structure, filling missing fields with `None`

#### Example 3: Custom Configuration

You can also run examples with custom configurations:

```bash
# Create your own configuration file
cat > custom_config.yaml << EOF
version: 0.1
kind: serializer

inline:
  - name: id
    from: id
    transform:
      type: integer
  - name: name
    from: name
    transform:
      type: string
  - name: status_label
    from: status
    transform:
      type: case
      cases:
        - when:
            field: status
            equals: "ACTIVE"
          then: "Running"
        - when:
            field: status
            equals: "PAUSED"
          then: "Stopped"
      default: "Unknown"
EOF

# Run with custom config (modify example script to use your config)
python -c "
import os
os.environ['ADAPT_CONFIGS'] = ''
from adapt.serializer.serializer import Serializer
from adapt.utils.config_reader import YamlReader

data = [{'id': '123', 'name': 'Test Campaign', 'status': 'ACTIVE'}]
config = YamlReader.read('custom_config.yaml')
serializer = Serializer.init(config)

for record in serializer.serialize_records(data):
    print(record)
"
```

#### Understanding Example Configurations

##### JSON Pipeline Configuration (`configs/json_pipeline.yaml`)

Key features demonstrated:
```yaml
# Enum mapping for status codes
- name: status_code
  from: status
  transform:
    type: enum
    mappings:
      ACTIVE: a
      PAUSED: p
      ARCHIVED: p

# Conditional budget handling
- name: budget
  transform:
    type: case
    cases:
      - when:
          field: lifetime_budget
          not_in: ["", 0, " ", "0"]
        then:
          type: currency
          multiplier: 100
      - when:
          field: daily_budget
          not_in: ["", 0, " ", "0"]
        then:
          type: currency
          multiplier: 100
    default: null

# Date parsing with custom format
- name: created_date
  from: created_time
  transform:
    type: date_parser
    format:
      output: "%Y-%m-%d"
```

##### Dictionary Normalization Configuration (`configs/dict_normalization.yaml`)

Key features demonstrated:
```yaml
# Nested object access
- name: campaign_id
  from: id
  transform:
    type: integer

# Array processing with extended mode
- name: adset_id
  from: id
  object: ad_sets
  transform:
    type: integer
  array:
    mode: extended

# Conditional field inclusion
- name: optimization_goal
  from: optimization_goal
  object: ad_sets.promoted_object
  transform:
    type: string
  ignore:
    when:
      field: optimization_goal
      in: ["", null]
```

#### Troubleshooting Examples

**Common issues and solutions:**

1. **Import errors:**
   ```bash
   # Ensure you're in the correct directory
   cd ADaPT-ETL/adapt/serializer
   
   # Verify installation
   pip show adapt-serializer adapt-utils
   ```

2. **Configuration not found:**
   ```bash
   # Check if config files exist
   ls -la examples/configs/
   
   # Verify file paths in example scripts
   ```

3. **Environment variable issues:**
   ```bash
   # The examples set ADAPT_CONFIGS="" to use inline configs
   # This is handled automatically in the example scripts
   ```

4. **Permission errors:**
   ```bash
   # Make sure example scripts are executable
   chmod +x examples/*.py
   ```

#### Creating Your Own Examples

To create custom examples:

1. **Create a configuration file:**
   ```yaml
   version: 0.1
   kind: serializer
   
   inline:
     - name: your_field
       from: source_field
       transform:
         type: string
   ```

2. **Create sample data:**
   ```python
   sample_data = [
       {"source_field": "value1"},
       {"source_field": "value2"}
   ]
   ```

3. **Run transformation:**
   ```python
   import os
   os.environ["ADAPT_CONFIGS"] = ""
   
   from adapt.serializer.serializer import Serializer
   from adapt.utils.config_reader import YamlReader
   
   config = YamlReader.read("your_config.yaml")
   serializer = Serializer.init(config)
   
   for record in serializer.serialize_records(sample_data):
       print(record)
   ```

### Custom Example

```python
import os
os.environ['ADAPT_CONFIGS'] = ''  # Use inline config

from adapt.serializer.serializer import Serializer

# Sample API response data
api_data = [
    {
        "id": "123",
        "name": "Holiday Campaign",
        "status": "ENABLED",
        "budget": {"amount_micros": 50000000},
        "metrics": {"impressions": 10000, "clicks": 500}
    },
    {
        "id": "456", 
        "name": "Brand Awareness",
        "status": "PAUSED",
        "budget": {"amount_micros": 30000000},
        "metrics": {"impressions": 5000, "clicks": 100}
    }
]

# Transformation configuration
config = {
    "inline": [
        {
            "name": "campaign_id",
            "from": "id",
            "transform": {"type": "integer"}
        },
        {
            "name": "campaign_name",
            "from": "name", 
            "transform": {"type": "string"}
        },
        {
            "name": "status_label",
            "from": "status",
            "transform": {
                "type": "case",
                "cases": [
                    {"when": {"field": "status", "equals": "ENABLED"}, "then": "Active"},
                    {"when": {"field": "status", "equals": "PAUSED"}, "then": "Paused"}
                ],
                "default": "Unknown"
            }
        },
        {
            "name": "daily_budget",
            "from": "amount_micros",
            "object": "budget",
            "transform": {"type": "float"}
        },
        {
            "name": "impressions",
            "from": "impressions",
            "object": "metrics",
            "transform": {"type": "integer"}
        }
    ],
    "derived": [
        {
            "name": "budget_category",
            "transform": {
                "type": "case",
                "cases": [
                    {"when": {"field": "daily_budget", "greater_than": 40000000}, "then": "High"},
                    {"when": {"field": "daily_budget", "greater_than": 20000000}, "then": "Medium"}
                ],
                "default": "Low"
            }
        }
    ]
}

# Create serializer and transform data
serializer = Serializer.init(config)
transformed_data = list(serializer.serialize_records(api_data))

for record in transformed_data:
    print(record)
```

## 🔗 Integration with Other Packages

### With Connector Package
```python
from adapt.connector.dispatcher import Dispatcher
from adapt.serializer.serializer import Serializer

# Get raw data from connector
raw_data = Dispatcher.receive(client, connector_config, external_input)

# Transform with serializer
serializer = Serializer.init(serializer_config)
transformed_data = serializer.serialize_records(raw_data)
```

### With Utils Package
```python
from adapt.utils.config_reader import YamlReader
from adapt.utils.exporter import CSVExporter
from adapt.serializer.serializer import Serializer

# Load configuration
config = YamlReader.load_from_config_location(
    module="serializer",
    namespace="your_service",
    config_name="campaign.yaml"
)

# Transform data
serializer = Serializer.init(config)
transformed_data = serializer.serialize_records(raw_data)

# Export results
export_config = {
    "export": {
        "filename": "transformed_campaigns",
        "fields": ["campaign_id", "campaign_name", "status"],
        "unique_on": ["campaign_id"]
    }
}
CSVExporter.lazy_run(export_config, transformed_data)
```

### With Pipeline Package
```python
from adapt.pipeline.pipeline import Pipeline, Item
from adapt.serializer.serializer import Serializer

# Serializer as a pipeline item
pipeline = Pipeline()
pipeline.add_item(Item(
    name="data_transformation",
    processor=Serializer.lazy_run,
    arguments={
        "config": serializer_config,
        "records": raw_data
    }
))
```

## 📖 API Reference

### Classes

#### `Serializer`
- `__init__(serializer, dict_normalize=False)` - Initialize with serializer typing
- `serialize(row, store=None)` - Transform a single record
- `serialize_records(records)` - Transform multiple records (generator)
- `init(config, dict_normalize=False)` - Class method to create from config
- `lazy_run(config, records, dict_normalize=False)` - One-shot transformation

### Configuration Schema

#### Main Configuration
```yaml
version: 0.1
kind: serializer

inline:                                               # Field mappings and transformations
  - name: string                                      # Output field name
    from: string                                      # Source field name (optional)
    object: string                                    # Nested object path (optional)
    transform:                                        # Transformation definition
      type: string
      # Type-specific parameters
    ignore:                                           # Conditional exclusion (optional)
      when:
        field: string
        equals|greater_than|less_than: value

derived:                                              # Computed fields
  - name: string
    transform:
      type: string
      # Transformation logic

constants:                                            # Static values
  - name: string
    value: any
    transform:
      type: string

export:                                               # Export configuration (optional)
  filename: string
  fields: [string]
  unique_on: [string]
```

#### Transformation Types
- `string` - Convert to string
- `integer` - Convert to integer
- `float` - Convert to float with precision
- `boolean` - Convert to boolean
- `case` - Conditional transformation with multiple cases
- `enum` - Map values using predefined mappings
- `constant` - Static value
- `currency` - Apply multiplier for currency conversion
- `date` - Convert date formats
- `ratio` - Calculate ratios between fields

## 🤝 Contributing

This package is part of the ADaPT ecosystem. See the main project's contributing guidelines.

## 📄 License

Licensed under the Apache License 2.0. See the [LICENSE](LICENSE) file for details.

---

**Part of the ADaPT Ecosystem** - Transforming data with flexibility and power. 🔄
