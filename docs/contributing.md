---
layout: default
title: Contributing
nav_order: 6
description: "How to contribute to ADaPT"
permalink: /contributing/
---

# Contributing to ADaPT

We welcome contributions to the ADaPT (Adaptive Data Pipeline Toolkit) project! This guide will help you get started with contributing code, documentation, bug reports, and feature requests.

## 🚀 Getting Started

### Prerequisites

- Python 3.7 or higher
- Git
- Basic understanding of data pipelines and ETL processes
- Familiarity with YAML configuration files

### Development Environment Setup

1. **Fork the repository** on GitHub
2. **Clone your fork** locally:
   ```bash
   git clone https://github.com/YOUR_USERNAME/ADaPT-ETL.git
   cd ADaPT-ETL
   ```

3. **Set up development environment**:
   ```bash
   # Create virtual environment
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   
   # Install in development mode
   make install MODE=dev
   
   # Verify installation
   adapt_pipeline --help
   ```

4. **Set up environment variables**:
   ```bash
   export ADAPT_CONFIGS="$(pwd)/configs"
   export ADAPT_OUTPUT_DIR="/tmp/adapt_dev"
   ```

## 📝 Types of Contributions

### 🐛 Bug Reports

When reporting bugs, please include:

- **Clear description** of the issue
- **Steps to reproduce** the problem
- **Expected vs actual behavior**
- **Environment details** (Python version, OS, package versions)
- **Error messages** and stack traces
- **Configuration files** (sanitized of sensitive data)

**Bug Report Template:**
```markdown
## Bug Description
Brief description of the issue

## Steps to Reproduce
1. Step one
2. Step two
3. Step three

## Expected Behavior
What should happen

## Actual Behavior
What actually happens

## Environment
- Python version: 
- OS: 
- ADaPT version: 
- Package versions: `pip list | grep adapt`

## Error Messages
```
Paste error messages here
```

## Configuration
```yaml
# Paste relevant configuration (remove sensitive data)
```
```

### ✨ Feature Requests

For feature requests, please provide:

- **Clear description** of the proposed feature
- **Use case** and motivation
- **Proposed implementation** (if you have ideas)
- **Examples** of how it would be used

### 📚 Documentation Improvements

Documentation contributions are highly valued! You can help by:

- Fixing typos and grammar
- Improving existing documentation
- Adding examples and tutorials
- Creating package-specific guides
- Updating API documentation

### 🔧 Code Contributions

We welcome code contributions including:

- Bug fixes
- New features
- Performance improvements
- Test coverage improvements
- Code refactoring

## 🛠️ Development Guidelines

### Code Style

- Follow **PEP 8** Python style guidelines
- Use **meaningful variable and function names**
- Add **docstrings** to all public functions and classes
- Keep functions **small and focused**
- Use **type hints** where appropriate

**Example:**
```python
def load_configuration(module: str, namespace: str, config_name: str) -> dict:
    """
    Load configuration from the ADaPT configuration structure.
    
    Args:
        module: Module name (connector, serializer, authorization, pipeline)
        namespace: Service namespace
        config_name: Configuration file name
        
    Returns:
        Parsed configuration dictionary
        
    Raises:
        FileNotFoundError: If configuration file is not found
        yaml.YAMLError: If YAML parsing fails
    """
    # Implementation here
```

### Testing

- **Write tests** for new functionality
- **Update existing tests** when modifying code
- **Ensure all tests pass** before submitting
- **Test with different Python versions** if possible

```bash
# Run tests (when test suite is available)
python -m pytest tests/

# Test installation
make verify

# Test CLI functionality
adapt_pipeline --help
```

### Configuration Guidelines

When adding new configuration options:

- **Use clear, descriptive names**
- **Provide sensible defaults**
- **Document all options**
- **Include examples**
- **Validate configuration** at runtime

**Example Configuration:**
```yaml
# Good: Clear structure and documentation
version: 1.0
kind: connector
description: "Example API connector with rate limiting"

client:
  type: rest_api
  base_url: "https://api.example.com"
  timeout: 30
  rate_limit:
    requests_per_second: 10
    burst_limit: 50

# Include inline documentation
# timeout: Request timeout in seconds (default: 30)
# rate_limit: Optional rate limiting configuration
```

### Package Structure

When adding new packages or modules:

- Follow the existing **package structure**
- Include **setup.py** and **pyproject.toml**
- Add **README.md** with package documentation
- Include **LICENSE** file
- Add **Makefile** for build automation

```
new_package/
├── LICENSE
├── Makefile
├── README.md
├── pyproject.toml
├── setup.py
└── new_package/
    ├── __init__.py
    ├── core_module.py
    └── utils.py
```

## 🔄 Contribution Workflow

### 1. Create a Branch

```bash
# Create and switch to a new branch
git checkout -b feature/your-feature-name

# Or for bug fixes
git checkout -b fix/issue-description
```

### 2. Make Changes

- Make your changes following the guidelines above
- Test your changes thoroughly
- Update documentation as needed

### 3. Commit Changes

Use clear, descriptive commit messages:

```bash
# Good commit messages
git commit -m "Add support for custom authentication headers in connector"
git commit -m "Fix serializer enum mapping for null values"
git commit -m "Update installation documentation with Docker examples"

# Follow conventional commits format (optional but preferred)
git commit -m "feat: add custom authentication header support"
git commit -m "fix: handle null values in enum mapping"
git commit -m "docs: add Docker installation examples"
```

### 4. Push and Create Pull Request

```bash
# Push your branch
git push origin feature/your-feature-name

# Create pull request on GitHub
```

### Pull Request Guidelines

**Pull Request Template:**
```markdown
## Description
Brief description of changes

## Type of Change
- [ ] Bug fix
- [ ] New feature
- [ ] Documentation update
- [ ] Performance improvement
- [ ] Code refactoring

## Testing
- [ ] Tests pass locally
- [ ] New tests added (if applicable)
- [ ] Manual testing completed

## Documentation
- [ ] Documentation updated
- [ ] Examples added/updated
- [ ] API documentation updated

## Checklist
- [ ] Code follows style guidelines
- [ ] Self-review completed
- [ ] No sensitive data included
- [ ] Breaking changes documented
```

## 📦 Package-Specific Contributions

### adapt-utils

Focus areas:
- Configuration management improvements
- New export formats
- Type system enhancements
- Utility functions

### adapt-connector

Focus areas:
- New API integrations
- Authentication methods
- Request/response handling
- Error handling improvements

### adapt-serializer

Focus areas:
- New transformation types
- Performance optimizations
- Data validation
- Complex data structure handling

### adapt-pipeline

Focus areas:
- CLI improvements
- Pipeline orchestration features
- Error handling and logging
- Performance monitoring

## 🧪 Testing Your Contributions

### Manual Testing

```bash
# Test basic functionality
export ADAPT_CONFIGS="$(pwd)/configs"
adapt_pipeline --namespace test \
  --pipeline-config data_ingestion.yaml \
  --data-ingestion-config test_config.yaml \
  --auth-data api_key="test" \
  --external-input resource_id="123"

# Test with Docker
docker-compose build
docker-compose run --rm adapt-etl adapt_pipeline --help
```

### Integration Testing

- Test with **real API configurations** (using test accounts)
- Verify **end-to-end pipeline execution**
- Test **error handling scenarios**
- Validate **output data quality**

## 📋 Review Process

### What We Look For

- **Code quality** and adherence to guidelines
- **Test coverage** for new functionality
- **Documentation** completeness
- **Backward compatibility** (unless breaking change is justified)
- **Performance impact** consideration

### Review Timeline

- Initial review within **1-2 weeks**
- Follow-up reviews within **3-5 days**
- Merge after **approval from maintainers**

## 🏷️ Release Process

### Versioning

We follow [Semantic Versioning](https://semver.org/):

- **MAJOR** version for incompatible API changes
- **MINOR** version for backward-compatible functionality
- **PATCH** version for backward-compatible bug fixes

### Release Checklist

- [ ] All tests pass
- [ ] Documentation updated
- [ ] Version numbers updated
- [ ] Changelog updated
- [ ] Release notes prepared

## 🤝 Community Guidelines

### Code of Conduct

- Be **respectful** and **inclusive**
- **Help others** learn and grow
- **Provide constructive feedback**
- **Focus on the code**, not the person

### Communication

- **GitHub Issues** for bug reports and feature requests
- **Pull Request discussions** for code review
- **Documentation** for questions about usage

## 🆘 Getting Help

If you need help with contributing:

1. **Check existing documentation** and examples
2. **Search existing issues** for similar problems
3. **Create a new issue** with your question
4. **Join discussions** on existing pull requests

## 📚 Resources

- **[Installation Guide]({{ site.baseurl }}/installation)** - Setup and installation
- **[Examples]({{ site.baseurl }}/examples)** - Usage examples and tutorials
- **[API Reference]({{ site.baseurl }}/api-reference)** - Complete API documentation
- **[Package Documentation]({{ site.baseurl }}/packages/)** - Package-specific guides

---

Thank you for contributing to ADaPT! Your contributions help make data pipeline development more accessible and powerful for everyone. 🚀 