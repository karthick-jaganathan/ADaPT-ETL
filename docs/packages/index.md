---
layout: page
title: Packages
nav_order: 3
description: "Documentation for all ADaPT packages"
permalink: /packages/
has_children: true
---

# ADaPT Packages

ADaPT consists of four core packages that work together to provide a complete data pipeline framework. Each package serves a specific purpose and can be used independently or as part of the complete toolkit.

## Package Overview

| Package | Purpose | Key Components |
|---------|---------|----------------|
| **[adapt-utils]({{ site.baseurl }}/packages/utils)** | Core utilities and configuration management | Config readers, data store, type system, exporters |
| **[adapt-connector]({{ site.baseurl }}/packages/connector)** | Data source connections and API integrations | Authorization, service clients, dispatchers, post-processors |
| **[adapt-serializer]({{ site.baseurl }}/packages/serializer)** | Data transformation and serialization | Field mapping, data normalization, conditional transformations |
| **[adapt-pipeline]({{ site.baseurl }}/packages/pipeline)** | Pipeline orchestration and execution | CLI interface, workflow management, pipeline items |

## Architecture Diagram

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

## Package Dependencies

The packages have the following dependency relationships:

- **adapt-utils**: No dependencies (foundation package)
- **adapt-connector**: Depends on adapt-utils
- **adapt-serializer**: Depends on adapt-utils
- **adapt-pipeline**: Depends on adapt-utils, adapt-connector, and adapt-serializer

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

## Quick Links

- **[Utils Package]({{ site.baseurl }}/packages/utils)** - Configuration, data storage, and utilities
- **[Connector Package]({{ site.baseurl }}/packages/connector)** - API connections and authorization
- **[Serializer Package]({{ site.baseurl }}/packages/serializer)** - Data transformation and mapping
- **[Pipeline Package]({{ site.baseurl }}/packages/pipeline)** - Orchestration and CLI tools

For detailed installation instructions, see the [Installation Guide]({{ site.baseurl }}/installation). 