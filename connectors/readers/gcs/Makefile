# ADaPT gcs connector Makefile

# Package information
PACKAGE_NAME = adapt-gcs
VERSION = 0.0.1

# Installation mode (dev, prod, dist)
MODE ?= prod

# Distribution directory
DIST_DIR = /tmp/sdist/adapt

.PHONY: install uninstall clean test help verify build

# Default target
help:
	@echo "ADaPT gcs connector - Available commands:"
	@echo ""
	@echo "  make install [MODE=dev|prod|dist] - Install $(PACKAGE_NAME) package (default: prod)"
	@echo "  make uninstall                    - Uninstall $(PACKAGE_NAME) package"
	@echo "  make build                        - Build distribution packages"
	@echo "  make clean                        - Clean build artifacts"
	@echo "  make test                         - Run the connector's tests"
	@echo "  make verify                       - Verify installation"
	@echo "  make help                         - Show this help message"
	@echo ""
	@echo "Installation modes:"
	@echo "  MODE=dev   - Development mode (pip install -e .)"
	@echo "  MODE=prod  - Production mode (pip install .)"
	@echo "  MODE=dist  - Distribution mode (build + install from $(DIST_DIR))"

# Install package with mode support
install:
ifeq ($(MODE),dev)
	@echo "Installing $(PACKAGE_NAME) package in development mode..."
	pip install -e .
else ifeq ($(MODE),prod)
	@echo "Installing $(PACKAGE_NAME) package in production mode..."
	pip install .
else ifeq ($(MODE),dist)
	@echo "Installing $(PACKAGE_NAME) package from distribution..."
	@$(MAKE) build
	@echo "Installing from distribution directory $(DIST_DIR)..."
	pip install $(PACKAGE_NAME) --find-links $(DIST_DIR) --force-reinstall
else
	@echo "❌ Invalid MODE: $(MODE). Use dev, prod, or dist"
	@exit 1
endif
	@echo "✅ Installation complete!"
	@echo "Installed packages:"
	@pip list | grep adapt

# Build distribution packages
build: clean
	@echo "Building distribution packages for $(PACKAGE_NAME)..."
	@echo "Creating distribution directory $(DIST_DIR)..."
	@mkdir -p $(DIST_DIR)
	@echo "Building distributions directly to $(DIST_DIR)..."
	python -m build --outdir $(DIST_DIR)
	@echo "✅ Build complete!"
	@echo "Distribution files in $(DIST_DIR):"
	@ls -la $(DIST_DIR)/

# Uninstall package
uninstall:
	@echo "Uninstalling $(PACKAGE_NAME) package..."
	pip uninstall -y $(PACKAGE_NAME)
	@echo "✅ Uninstallation complete!"

# Clean build artifacts
clean:
	@echo "Cleaning build artifacts for $(PACKAGE_NAME)..."
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "build" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "dist" -exec rm -rf {} + 2>/dev/null || true
	@echo "✅ Cleanup complete!"

# Run tests (offline: object storage is simulated; set ADAPT_TEST_GCS for a real bucket)
test:
	@echo "Running tests for $(PACKAGE_NAME)..."
	python -m pytest tests -q

# Quick verification
verify:
	@echo "Verifying $(PACKAGE_NAME) installation..."
	@pip show $(PACKAGE_NAME) >/dev/null 2>&1 && echo "✅ $(PACKAGE_NAME) is installed!" || echo "❌ $(PACKAGE_NAME) is not installed!"
	@python -c "import adapt.connectors.gcs.connector; print('✅ adapt.connectors.gcs import works!')" 2>/dev/null || echo "❌ adapt.connectors.gcs import failed!"
	@adapt connectors | grep -qE '^gcs( |$$)' && echo "✅ gcs connector is registered!" || echo "❌ gcs connector is not registered!" 