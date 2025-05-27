# ADaPT Serializer Makefile

# Package information
PACKAGE_NAME = adapt-serializer
VERSION = 0.0.1

# Installation mode (dev, prod, dist)
MODE ?= prod

# Distribution directory
DIST_DIR = /tmp/sdist/adapt

# Force dependency reinstallation (useful when dependencies have code changes)
FORCE_DEPS ?= false

.PHONY: install uninstall clean test help verify build

# Default target
help:
	@echo "ADaPT Serializer - Available commands:"
	@echo ""
	@echo "  make install [MODE=dev|prod|dist] - Install $(PACKAGE_NAME) package (default: prod)"
	@echo "  make uninstall                    - Uninstall $(PACKAGE_NAME) package"
	@echo "  make build                        - Build distribution packages"
	@echo "  make clean                        - Clean build artifacts"
	@echo "  make test                         - Run tests (if available)"
	@echo "  make verify                       - Verify installation"
	@echo "  make help                         - Show this help message"
	@echo ""
	@echo "Installation modes:"
	@echo "  MODE=dev   - Development mode (pip install -e .)"
	@echo "  MODE=prod  - Production mode (pip install .)"
	@echo "  MODE=dist  - Distribution mode (build + install from $(DIST_DIR))"

# Install package in development mode with dependencies
install: _install-deps
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

# Install dependencies
_install-deps:
	@echo "Checking dependencies for $(PACKAGE_NAME)..."
	@if [ "$(FORCE_DEPS)" = "true" ] || ! pip show adapt-utils >/dev/null 2>&1; then \
		echo "Installing adapt-utils..."; \
		cd ../utils && $(MAKE) install MODE=$(MODE); \
	else \
		echo "✅ adapt-utils already installed"; \
	fi

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

# Run tests (placeholder)
test:
	@echo "Running tests for $(PACKAGE_NAME)..."
	@echo "⚠️  No tests configured yet. Add your test commands here."
	# pytest tests/ --verbose

# Quick verification
verify:
	@echo "Verifying $(PACKAGE_NAME) installation..."
	@pip show $(PACKAGE_NAME) >/dev/null 2>&1 && echo "✅ $(PACKAGE_NAME) is installed!" || echo "❌ $(PACKAGE_NAME) is not installed!"
	@pip show adapt-utils >/dev/null 2>&1 && echo "✅ adapt-utils dependency is installed!" || echo "❌ adapt-utils dependency is missing!"
	@ADAPT_CONFIGS="$$(pwd)/../../configs" python -c "import adapt.serializer; print('✅ adapt.serializer import works!')" 2>/dev/null || echo "❌ adapt.serializer import failed!" 