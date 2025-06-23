---
layout: default
title: Installation
nav_order: 2
description: "Comprehensive installation guide for ADaPT (Adaptive Data Pipeline Toolkit)"
permalink: /installation/
---

# ADaPT Installation Guide

**📖 Comprehensive installation guide for ADaPT (Adaptive Data Pipeline Toolkit)**

{: .important }
> **Before you begin:** Ensure you have Python 3.7+ installed and a basic understanding of command-line operations.

This guide provides detailed installation instructions, multiple installation methods, environment setup, troubleshooting, and development setup.

## 📋 Table of Contents

- [Prerequisites](#prerequisites)
- [Installation Methods](#installation-methods)
- [Environment Setup](#environment-setup)
- [Verification](#verification)
- [Uninstallation](#uninstallation)
- [Troubleshooting](#troubleshooting)
- [Development Setup](#development-setup)

## Prerequisites

### System Requirements

- **Python**: 3.7 or higher
- **Operating System**: Linux, macOS, or Windows
- **Memory**: Minimum 512MB RAM
- **Disk Space**: At least 100MB free space

### Python Dependencies

ADaPT automatically installs the following core dependencies:

- `pyyaml==6.0.2` - YAML configuration parsing
- `python-dateutil>=2.8.0` - Date/time utilities

### Optional Dependencies

For specific integrations, install additional packages as needed:

```bash
# REST API integrations
pip install requests>=2.25.0

# Database connections
pip install sqlalchemy>=1.4.0

# MongoDB integration
pip install pymongo>=3.12.0

# PostgreSQL connections
pip install psycopg2-binary>=2.9.0
```

or use the provided `requirements.txt`:

```bash
pip install -r requirements.txt
```

Use the `requirements-dev.txt` for managing dependencies


## Installation Methods

### Method 1: Complete Installation (Recommended)

This method installs all ADaPT packages and provides the full functionality.

```bash
# Clone the repository
git clone https://github.com/karthick-jaganathan/ADaPT-ETL.git
cd ADaPT-ETL

# Install using Makefile (recommended)
make install

# Alternative: Install individual packages directly
cd adapt/utils && pip install .
cd ../connector && pip install .
cd ../serializer && pip install .
cd ../pipeline && pip install .
```

**What gets installed:**
- `adapt-utils` (0.0.1) - Core utilities
- `adapt-connector` (0.0.1) - Data connectors
- `adapt-serializer` (0.0.1) - Data serialization
- `adapt-pipeline` (0.0.1) - Pipeline orchestration (includes CLI tools)

### Method 2: Development Installation

For developers who want to modify the code:

```bash
# Clone the repository
git clone https://github.com/karthick-jaganathan/ADaPT-ETL.git
cd ADaPT-ETL

# Install in development mode
make install MODE=dev

# Alternative: Install individual packages in development mode
cd adapt/utils && pip install -e .
cd ../connector && pip install -e .
cd ../serializer && pip install -e .
cd ../pipeline && pip install -e .
```

**Benefits of development mode:**
- Changes to source code are immediately reflected
- No need to reinstall after code modifications
- Easier debugging and testing

### Method 3: Individual Package Installation

Install only the components you need:

```bash
# Clone the repository
git clone https://github.com/karthick-jaganathan/ADaPT-ETL.git
cd ADaPT-ETL

# Install packages in dependency order
cd adapt/utils && pip install .          # Core utilities (required by all)
cd ../connector && pip install .         # Data connectors
cd ../serializer && pip install .        # Data serialization
cd ../pipeline && pip install .          # Pipeline orchestration (includes CLI)
```

**Use cases:**
- Minimal installations for specific use cases
- Custom integrations requiring only certain components
- Resource-constrained environments

### Method 4: Distribution Installation

Build and install from distribution packages:

```bash
# Clone and build
git clone https://github.com/karthick-jaganathan/ADaPT-ETL.git
cd ADaPT-ETL

# Build all distributions
make build

# Install from built distributions
make install MODE=dist
```

**When to use:**
- Production deployments
- Offline installations
- Package distribution to other systems

### Method 5: Docker Installation

Use Docker for containerized deployment with all dependencies pre-installed:

```bash
# Using Docker Compose (recommended)
git clone https://github.com/karthick-jaganathan/ADaPT-ETL.git
cd ADaPT-ETL

# Build the container (includes all ADaPT packages)
docker-compose build

# Start container in background
docker-compose up -d

# Test the installation (using exec with running container)
docker-compose exec adapt-etl adapt_pipeline --help

# Alternative: Test with run (creates new container)
docker-compose run --rm adapt-etl adapt_pipeline --help

# Using Docker directly
docker build -t adapt-etl .
docker run -it --rm adapt-etl adapt_pipeline --help
```

**Note**: The Docker setup automatically installs all required system dependencies (including `make`, `gcc`, `g++`) and ADaPT packages in development mode.

**Docker Command Differences**:
- `docker-compose run --rm`: Creates a new container, runs the command, and removes the container when done
- `docker-compose exec`: Executes a command in an already running container (requires `docker-compose up -d` first)
- `docker-compose up -d`: Starts the container in the background and keeps it running

## Environment Setup

### Required Environment Variables

ADaPT requires specific environment variables to function properly:

```bash
# Required: Path to configuration files
export ADAPT_CONFIGS="/path/to/your/configs"

# Optional: Output directory for processed data (defaults to /tmp)
export ADAPT_OUTPUT_DIR="/path/to/output"

# Example for development
export ADAPT_CONFIGS="$(pwd)/configs"
export ADAPT_OUTPUT_DIR="/data/adapt_etl"
```

### Configuration Directory Structure

Ensure your configuration directory follows this structure:

```
configs/
├── pipeline/
│   └── data_ingestion.yaml
├── connector/
│   ├── your_service/
│   │   └── data_config.yaml
│   └── another_api/
│       └── endpoint_config.yaml
├── serializer/
│   ├── your_service/
│   │   └── data_transform.yaml
│   └── another_api/
│       └── data_mapping.yaml
└── authorization/
    ├── your_service/
    │   └── authorization.yaml
    └── another_api/
        └── authorization.yaml
```

### Shell Configuration

Add environment variables to your shell profile for persistence:

```bash
# For bash users (~/.bashrc or ~/.bash_profile)
echo 'export ADAPT_CONFIGS="/path/to/your/configs"' >> ~/.bashrc
echo 'export ADAPT_OUTPUT_DIR="/path/to/output"' >> ~/.bashrc
source ~/.bashrc

# For zsh users (~/.zshrc)
echo 'export ADAPT_CONFIGS="/path/to/your/configs"' >> ~/.zshrc
echo 'export ADAPT_OUTPUT_DIR="/path/to/output"' >> ~/.zshrc
source ~/.zshrc
```

## Verification

### Basic Verification

```bash
# Check all packages are installed
make verify

# Manual verification
pip list | grep adapt
```

Expected output:
```
adapt-connector          0.0.1
adapt-pipeline           0.0.1
adapt-serializer         0.0.1
adapt-utils              0.0.1
```

### CLI Tool Verification

```bash
# Test CLI tool availability
adapt_pipeline --help

# Test with environment variables
ADAPT_CONFIGS="$(pwd)/configs" adapt_pipeline --help
```

### Import Verification

```bash
# Test Python imports
python -c "
import adapt.utils
import adapt.connector
import adapt.serializer
import adapt.pipeline
print('✅ All packages imported successfully!')
"
```

### Functional Verification

```bash
# Test with sample configuration (if available)
cd ADaPT-ETL
ADAPT_CONFIGS="$(pwd)/configs" adapt_pipeline --namespace your_service \
  --pipeline-config data_ingestion.yaml \
  --data-ingestion-config data_config.yaml \
  --help
```

## Uninstallation

### Complete Uninstallation

```bash
# Using Makefile (recommended)
make uninstall

# Manual uninstallation
pip uninstall -y adapt-utils adapt-connector adapt-serializer adapt-pipeline
```

### Partial Uninstallation

Remove specific packages while keeping others:

```bash
# Remove only the pipeline package
pip uninstall adapt-pipeline

# Remove connector and serializer but keep utils
pip uninstall adapt-connector adapt-serializer
```

### Clean Uninstallation

Remove all traces including build artifacts:

```bash
# Uninstall packages
make uninstall

# Clean build artifacts
make clean

# Remove cloned repository (optional)
cd .. && rm -rf ADaPT-ETL
```

## Troubleshooting

### Common Issues and Solutions

#### 1. Command Not Found: `adapt_pipeline`

**Problem**: `bash: adapt_pipeline: command not found`

**Solutions**:
```bash
# Check if packages are installed
pip list | grep adapt

# Reinstall if missing
make install

# Check Python PATH
python -c "import sys; print(sys.path)"

# Verify virtual environment is activated
which python
```

#### 2. Environment Variable Error

**Problem**: `'ADAPT_CONFIGS' path environment variable not set`

**Solutions**:
```bash
# Set environment variable
export ADAPT_CONFIGS="/path/to/configs"

# Verify it's set
echo $ADAPT_CONFIGS

# Add to shell profile for persistence
echo 'export ADAPT_CONFIGS="/path/to/configs"' >> ~/.bashrc
```

#### 3. Missing Dependencies

**Problem**: `ModuleNotFoundError: No module named 'adapt.utils'`

**Solutions**:
```bash
# Check what's installed
pip list | grep adapt

# Reinstall all packages
make uninstall
make install

# Install dependencies manually if needed
cd adapt/utils && pip install .
cd ../connector && pip install .
cd ../serializer && pip install .
cd ../pipeline && pip install .
```

#### 4. Permission Errors

**Problem**: `Permission denied` during installation

**Solutions**:
```bash
# Use virtual environment (recommended)
python -m venv adapt_env
source adapt_env/bin/activate  # On Windows: adapt_env\Scripts\activate
pip install .

# Or use user installation
pip install --user .

# Or use sudo (not recommended)
sudo pip install .
```

#### 5. Build Failures

**Problem**: Build or installation fails

**Solutions**:
```bash
# Clean previous builds
make clean

# Update pip and setuptools
pip install --upgrade pip setuptools wheel

# Install with verbose output
pip install -v .

# Check for system dependencies
# On Ubuntu/Debian:
sudo apt-get update
sudo apt-get install python3-dev build-essential

# On macOS:
xcode-select --install
```

#### 6. Import Errors

**Problem**: `ImportError` when importing packages

**Solutions**:
```bash
# Check Python version
python --version  # Should be 3.7+

# Check package installation location
python -c "import adapt.utils; print(adapt.utils.__file__)"

# Reinstall in correct environment
pip uninstall adapt-utils adapt-connector adapt-serializer adapt-pipeline
pip install .
```

#### 7. Docker Build Issues

**Problem**: `make: not found` during Docker build

**Solutions**:
```bash
# This issue has been resolved in the current Dockerfile
# The Dockerfile now includes 'make' in system dependencies

# If you encounter this issue with older versions:
# Option 1: Update to latest version
git pull origin main
docker-compose build

# Option 2: Use direct pip installation (modify Dockerfile)
# Replace: RUN make install MODE=dev
# With: RUN cd adapt/utils && pip install -e . && \
#           cd ../connector && pip install -e . && \
#           cd ../serializer && pip install -e . && \
#           cd ../pipeline && pip install -e .
```

**Problem**: Docker build fails or takes too long

**Solutions**:
```bash
# Clean Docker cache
docker system prune -a

# Build with no cache
docker-compose build --no-cache

# Check Docker resources (increase memory/CPU if needed)
docker system df

# Build individual steps for debugging
docker build --target adapt-etl .
```

**Problem**: `docker-compose exec` fails with "No such service" or container not running

**Solutions**:
```bash
# Make sure container is running first
docker-compose up -d

# Then use exec
docker-compose exec adapt-etl adapt_pipeline --help

# Alternative: Use run instead (doesn't require running container)
docker-compose run --rm adapt-etl adapt_pipeline --help

# Check container status
docker-compose ps
```

#### 8. Protobuf Version Compatibility Issues

**Problem**: `TypeError: MessageToDict() got an unexpected keyword argument 'including_default_value_fields'`

**Solutions**:
```bash
# This issue has been resolved in the current version
# The post_processor now automatically detects protobuf version and uses correct parameters

# If you encounter this issue with older versions:
# Option 1: Update to latest version
git pull origin main
make install MODE=dev

# Option 2: Check protobuf versions
pip show protobuf  # On host system
docker-compose run --rm adapt-etl pip show protobuf  # In container

# The fix automatically handles:
# - protobuf 4.x: uses 'including_default_value_fields' parameter
# - protobuf 5.x+: uses 'always_print_fields_with_no_presence' parameter
```

### Getting Help

If you encounter issues not covered here:

1. **Check the logs**: Use `pip install -v .` for verbose output
2. **Verify environment**: Ensure correct Python version and virtual environment
3. **Clean installation**: Try `make clean && make install`
4. **Check dependencies**: Ensure all system dependencies are installed
5. **Report issues**: Create an issue on [GitHub](https://github.com/karthick-jaganathan/ADaPT-ETL/issues)

## Development Setup

### Setting Up Development Environment

```bash
# Clone repository
git clone https://github.com/karthick-jaganathan/ADaPT-ETL.git
cd ADaPT-ETL

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install in development mode
make install MODE=dev

# Install additional development dependencies (if available)
pip install -r requirements-dev.txt  # If this file exists
```

### Development Commands

```bash
# Show all available commands
make help

# Install packages
make install [MODE=dev|prod|dist]

# Build distributions
make build [TYPE=sdist|wheel|all]

# Clean build artifacts
make clean

# Verify installation
make verify

# Uninstall packages
make uninstall
```

### Package-Specific Development

Work on individual packages:

```bash
# Utils package
cd adapt/utils
make install MODE=dev
make verify

# Connector package
cd adapt/connector
make install MODE=dev
make verify

# Serializer package
cd adapt/serializer
make install MODE=dev
make verify

# Pipeline package
cd adapt/pipeline
make install MODE=dev
make verify
```

### Testing Development Installation

```bash
# Test imports
python -c "
import adapt.utils
import adapt.connector
import adapt.serializer
import adapt.pipeline
print('Development installation successful!')
"

# Test CLI
adapt_pipeline --help

# Test with sample data (if available)
cd adapt/serializer
python examples/json_pipeline.py
```

---

## Installation Modes Summary

| Mode             | Command                             | Use Case             | Editable   | CLI Tools  |
|------------------|-------------------------------------|----------------------|------------|------------|
| **Production**   | `make install`                      | Production use       | No         | Yes        |
| **Development**  | `make install MODE=dev`             | Development          | Yes        | Yes        |
| **Distribution** | `make install MODE=dist`            | Package distribution | No         | Yes        |
| **Individual**   | `cd adapt/package && pip install .` | Minimal installation | No         | Partial    |
| **Docker**       | `docker-compose up`                 | Containerized        | No         | Yes        |

Choose the installation mode that best fits your use case and environment requirements.
