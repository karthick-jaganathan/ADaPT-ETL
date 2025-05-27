---
layout: home
title: Home
nav_order: 1
description: "ADaPT - Adaptive Data Pipeline Toolkit Documentation"
permalink: /
---

# ADaPT - Adaptive Data Pipeline Toolkit

**A Python-based, YAML-configured data pipeline framework for extracting, transforming, and exporting data from various sources to multiple destinations.**

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/python-3.7+-blue.svg)](https://www.python.org/downloads/)
[![GitHub](https://img.shields.io/badge/GitHub-ADaPT--ETL-blue.svg)](https://github.com/karthick-jaganathan/ADaPT-ETL)

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

## 📚 Documentation Sections

<div class="grid-container">
  <div class="grid-item">
    <h3><a href="{{ site.baseurl }}/installation">🛠️ Installation</a></h3>
    <p>Comprehensive installation guide with multiple methods, troubleshooting, and development setup.</p>
  </div>
  
  <div class="grid-item">
    <h3><a href="{{ site.baseurl }}/packages/">📦 Packages</a></h3>
    <p>Detailed documentation for all four core packages with API references and examples.</p>
  </div>
  
  <div class="grid-item">
    <h3><a href="{{ site.baseurl }}/examples">💡 Examples</a></h3>
    <p>Practical examples and tutorials for common use cases and integrations.</p>
  </div>
  
  <div class="grid-item">
    <h3><a href="{{ site.baseurl }}/api-reference">📖 API Reference</a></h3>
    <p>Complete API documentation with class references and method signatures.</p>
  </div>
</div>

## 🚀 Quick Start

### Installation

```bash
# Clone and install
git clone https://github.com/karthick-jaganathan/ADaPT-ETL.git
cd ADaPT-ETL
make install

# Verify installation
adapt_pipeline --help
```

### Basic Usage

```bash
# Set environment variables
export ADAPT_CONFIGS="$(pwd)/configs"
export ADAPT_OUTPUT_DIR="/path/to/output"

# Run your first pipeline
adapt_pipeline --namespace your_namespace \
  --pipeline-config data_ingestion.yaml \
  --data-ingestion-config your_config.yaml \
  --auth-data api_key="your-api-key"
```

## 🤝 Contributing

We welcome contributions! See our [Contributing Guide]({{ site.baseurl }}/contributing) for details on how to get started.

## 📄 License

This project is licensed under the Apache License 2.0 - see the [LICENSE](https://github.com/karthick-jaganathan/ADaPT-ETL/blob/master/LICENSE) file for details.

## 🆘 Support

- **GitHub Issues**: [Report bugs and request features](https://github.com/karthick-jaganathan/ADaPT-ETL/issues)
- **Documentation**: Browse the sections above for detailed guides
- **Examples**: Check out practical examples in the Examples section

---

<style>
.grid-container {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 20px;
  margin: 20px 0;
}

.grid-item {
  border: 1px solid #e1e4e8;
  border-radius: 6px;
  padding: 20px;
  background-color: #f6f8fa;
}

.grid-item h3 {
  margin-top: 0;
  margin-bottom: 10px;
}

.grid-item h3 a {
  text-decoration: none;
  color: #0366d6;
}

.grid-item h3 a:hover {
  text-decoration: underline;
}

.grid-item p {
  margin-bottom: 0;
  color: #586069;
}
</style> 