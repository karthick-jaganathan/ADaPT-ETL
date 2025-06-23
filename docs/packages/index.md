---
layout: default
title: Packages
nav_order: 3
description: "Documentation for all ADaPT packages"
permalink: /packages/
has_children: true
has_toc: false
---

# ADaPT Packages

ADaPT consists of four core packages that work together to provide a complete data pipeline framework. Each package serves a specific purpose and can be used independently or as part of the complete toolkit.

## Package Overview

| Package                                                        | Purpose                                      | Key Components                                                 |
|----------------------------------------------------------------|----------------------------------------------|----------------------------------------------------------------|
| **[adapt-utils]({{ site.baseurl }}/packages/utils)**           | Core utilities and configuration management  | Config readers, data store, type system, exporters             |
| **[adapt-connector]({{ site.baseurl }}/packages/connector)**   | Data source connections and API integrations | Authorization, service clients, dispatchers, post-processors   |
| **[adapt-serializer]({{ site.baseurl }}/packages/serializer)** | Data transformation and serialization        | Field mapping, data normalization, conditional transformations |
| **[adapt-pipeline]({{ site.baseurl }}/packages/pipeline)**     | Pipeline orchestration and execution         | CLI interface, workflow management, pipeline items             |

## Architecture Diagram

![ADaPT System Overview]({{ site.baseurl }}/assets/images/diagrams/system_overview.svg)

## Package Dependencies

The packages have the following dependency relationships:

- **[adapt-utils]({{ site.baseurl }}/packages/utils)**: required by all other packages, providing core utilities and configuration management.
- **[adapt-connector]({{ site.baseurl }}/packages/connector)**: Depends on adapt-utils, providing API connections and data extraction capabilities.
- **[adapt-serializer]({{ site.baseurl }}/packages/serializer)**: Depends on adapt-utils, providing data transformation and serialization features.
- **[adapt-pipeline]({{ site.baseurl }}/packages/pipeline)**: Depends on adapt-utils, adapt-connector, and adapt-serializer to orchestrate the entire data processing workflow.

## Installation Options

You can install packages individually or as a complete toolkit:

### Complete Installation
```bash
make install
```

### Individual Package Installation
```bash
# Install packages in dependency order
cd adapt/utils && pip install .
cd ../connector && pip install .
cd ../serializer && pip install .
cd ../pipeline && pip install .
```

### Development Installation
```bash
make install MODE=dev
```

For detailed installation instructions, see the [Installation Guide]({{ site.baseurl }}/installation). 