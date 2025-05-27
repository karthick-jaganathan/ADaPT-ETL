# ADaPT Makefile for convenient installation and management

# Default installation mode
MODE ?= prod

# Force dependency reinstallation (useful when dependencies have code changes)
FORCE_DEPS ?= false

# Package list in dependency order
PACKAGES = utils connector serializer pipeline

# Distribution base directory
DIST_BASE = /tmp/sdist/adapt

.PHONY: install install-all build build-all clean verify help uninstall clean-dist

# Default target
help:
	@echo "ADaPT (Adaptive Data Pipeline Toolkit) - Available commands:"
	@echo ""
	@echo "  make install [MODE=dev|prod|dist] - Install all packages (default: prod)"
	@echo "  make install FORCE_DEPS=true      - Force reinstall dependencies"
	@echo "  make build [TYPE=sdist|wheel|all] - Build distributions for all packages"
	@echo "  make uninstall                    - Uninstall all packages"
	@echo "  make clean                        - Clean build artifacts"
	@echo "  make clean-dist                   - Clean distribution directory"
	@echo "  make verify                       - Verify installation"
	@echo "  make help                         - Show this help"
	@echo ""
	@echo "Installation modes:"
	@echo "  MODE=prod  - Production mode (pip install .) [DEFAULT]"
	@echo "  MODE=dev   - Development mode (pip install -e .)"
	@echo "  MODE=dist  - Distribution mode (build + install from $(DIST_BASE))"
	@echo ""
	@echo "Individual packages: cd adapt/utils|connector|serializer|pipeline && make install"

# Generic install command for all packages
install: install-all
install-all:
	@echo "Installing all ADaPT packages in $(MODE) mode..."
ifeq ($(MODE),dist)
	@echo "Cleaning distribution directory first..."
	@$(MAKE) clean-dist
endif
	@for pkg in $(PACKAGES); do \
		echo "Installing $$pkg..."; \
		cd adapt/$$pkg && $(MAKE) install MODE=$(MODE) FORCE_DEPS=$(FORCE_DEPS) && cd ../..; \
	done
	@echo "✅ All ADaPT packages installed successfully!"
	@$(MAKE) _show-packages

# Individual package installation with mode support
install-utils:
	@echo "Installing utils in $(MODE) mode..."
	cd adapt/utils && $(MAKE) install MODE=$(MODE) FORCE_DEPS=$(FORCE_DEPS)

install-connector:
	@echo "Installing connector in $(MODE) mode..."
	cd adapt/connector && $(MAKE) install MODE=$(MODE) FORCE_DEPS=$(FORCE_DEPS)

install-serializer:
	@echo "Installing serializer in $(MODE) mode..."
	cd adapt/serializer && $(MAKE) install MODE=$(MODE) FORCE_DEPS=$(FORCE_DEPS)

install-pipeline:
	@echo "Installing pipeline in $(MODE) mode..."
	cd adapt/pipeline && $(MAKE) install MODE=$(MODE) FORCE_DEPS=$(FORCE_DEPS)

# Generic build command for all packages
build: build-all
build-all:
	@echo "Building distributions for all packages..."
	@echo "Cleaning distribution directory first..."
	@$(MAKE) clean-dist
	@for pkg in $(PACKAGES); do \
		echo "Building $$pkg..."; \
		cd adapt/$$pkg && $(MAKE) build && cd ../..; \
	done
	@echo "✅ All distributions built successfully!"
	@echo "Distribution files:"
	@find $(DIST_BASE) -name "*.tar.gz" -o -name "*.whl" 2>/dev/null | sort || echo "No distributions found"

# Utility commands
uninstall:
	@echo "Uninstalling all ADaPT packages..."
	pip uninstall -y adapt-pipeline adapt-connector adapt-serializer adapt-utils 2>/dev/null || true
	@echo "✅ All packages uninstalled!"

clean:
	@echo "Cleaning build artifacts..."
	@for pkg in $(PACKAGES); do \
		cd adapt/$$pkg && $(MAKE) clean && cd ../..; \
	done
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "build" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "dist" -exec rm -rf {} + 2>/dev/null || true
	@echo "✅ All artifacts cleaned!"

clean-dist:
	@echo "Cleaning distribution directory /tmp/sdist/adapt..."
	@rm -rf /tmp/sdist/adapt
	@echo "✅ Distribution directory cleaned!"

verify:
	@echo "Verifying all installations..."
	@for pkg in $(PACKAGES); do \
		echo ""; \
		cd adapt/$$pkg && $(MAKE) verify && cd ../..; \
	done
	@echo ""
	@echo "✅ All verifications completed!"

# Internal helper commands
_show-packages:
	@echo "Installed packages:"
	@pip list | grep adapt 