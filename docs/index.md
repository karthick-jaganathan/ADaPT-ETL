---
layout: default
title: Home
nav_order: 1
description: "ADaPT - Adaptive Data Pipeline Toolkit Documentation"
permalink: /
---

<div class="hero-section">
  <h1>🚀 ADaPT - Adaptive Data Pipeline Toolkit</h1>
  <p class="subtitle">A Python-based, YAML-configured data pipeline framework for extracting, transforming, and exporting data from various sources to multiple destinations.</p>
  
  <div class="badges">
    <img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg" alt="License">
    <img src="https://img.shields.io/badge/python-3.7+-blue.svg" alt="Python">
    <img src="https://img.shields.io/badge/GitHub-ADaPT--ETL-blue.svg" alt="GitHub">
  </div>
  
  <div>
    <a href="{{ site.baseurl }}/installation" class="btn btn-primary">Get Started</a>
    <a href="https://github.com/karthick-jaganathan/ADaPT-ETL" class="btn btn-outline">View on GitHub</a>
  </div>
</div>

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

<div class="architecture-diagram">
<pre>
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
</pre>
</div>

## 📚 Documentation Sections

<div class="feature-grid">
  <div class="feature-card">
    <h3><a href="{{ site.baseurl }}/installation">🛠️ Installation</a></h3>
    <p>Comprehensive installation guide with multiple methods, troubleshooting, and development setup.</p>
  </div>
  
  <div class="feature-card">
    <h3><a href="{{ site.baseurl }}/packages/">📦 Packages</a></h3>
    <p>Detailed documentation for all four core packages with API references and examples.</p>
  </div>
  
  <div class="feature-card">
    <h3><a href="{{ site.baseurl }}/examples">💡 Examples</a></h3>
    <p>Practical examples and tutorials for common use cases and integrations.</p>
  </div>
  
  <div class="feature-card">
    <h3><a href="{{ site.baseurl }}/api-reference">📖 API Reference</a></h3>
    <p>Complete API documentation with class references and method signatures.</p>
  </div>
</div>

## 🚀 Quick Start

{: .note }
> **Ready to get started?** Follow these simple steps to set up ADaPT and run your first data pipeline.

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