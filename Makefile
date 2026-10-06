# ADaPT Makefile for convenient installation and management

# Default installation mode
MODE ?= prod

# Force dependency reinstallation (useful when dependencies have code changes)
FORCE_DEPS ?= false

# The adapt-core package (the `adapt` CLI: run, validate, connectors), at adapt-core
CORE_DIR = adapt-core
# Connectors (optional: each installs its own dependencies: a vendor SDK, or DuckDB for files, s3, gcs and
# postgres), under connectors/{readers,ads}/<name>
CONNECTORS = files s3 gcs postgres google_ads microsoft_ads facebook_ads
CONNECTOR_DIRS = connectors/readers/files connectors/readers/s3 connectors/readers/gcs connectors/readers/postgres \
	connectors/ads/google_ads connectors/ads/microsoft_ads connectors/ads/facebook_ads

# Distribution base directory
DIST_BASE = /tmp/sdist/adapt

.PHONY: install install-all install-connectors build build-all clean verify verify-connectors help uninstall clean-dist \
	test validate

# Default target
help:
	@echo "ADaPT (Adaptive Data Pipeline Toolkit) - Available commands:"
	@echo ""
	@echo "  make install [MODE=dev|prod|dist]    - Install adapt-core (default: prod)"
	@echo "  make install FORCE_DEPS=true         - Force reinstall dependencies"
	@echo "  make install-connectors [MODE=...]   - Install the connectors: $(CONNECTORS)"
	@echo "  make build [TYPE=sdist|wheel|all]    - Build distributions for all packages"
	@echo "  make uninstall                       - Uninstall all packages"
	@echo "  make clean                           - Clean build artifacts"
	@echo "  make clean-dist                      - Clean distribution directory"
	@echo "  make verify                          - Verify installation"
	@echo "  make verify-connectors               - Verify the connectors"
	@echo "  make test                            - Run the test suite (needs pytest; jsonschema optional)"
	@echo "  make validate                        - Validate examples/ with adapt validate --strict"
	@echo "  make help                            - Show this help"
	@echo ""
	@echo "Installation modes:"
	@echo "  MODE=prod  - Production mode (pip install .) [DEFAULT]"
	@echo "  MODE=dev   - Development mode (pip install -e .)"
	@echo "  MODE=dist  - Distribution mode (build + install from $(DIST_BASE))"
	@echo ""
	@echo "Individual package: make install-core (or: cd adapt-core && make install)"
	@echo "Individual connectors: make install-google-ads | install-microsoft-ads | install-facebook-ads | install-files | install-s3 | install-gcs | install-postgres"

# Generic install command for all packages
install: install-all
install-all:
	@echo "Installing all ADaPT packages in $(MODE) mode..."
ifeq ($(MODE),dist)
	@echo "Cleaning distribution directory first..."
	@$(MAKE) clean-dist
endif
	cd $(CORE_DIR) && $(MAKE) install MODE=$(MODE) FORCE_DEPS=$(FORCE_DEPS)
	@echo "✅ All ADaPT packages installed successfully!"
	@$(MAKE) _show-packages

# Individual package installation with mode support
install-core:
	@echo "Installing adapt-core in $(MODE) mode..."
	cd $(CORE_DIR) && $(MAKE) install MODE=$(MODE) FORCE_DEPS=$(FORCE_DEPS)

install-connectors:
	@for dir in $(CONNECTOR_DIRS); do \
		echo "Installing $$dir..."; \
		(cd $$dir && $(MAKE) install MODE=$(MODE) FORCE_DEPS=$(FORCE_DEPS)) || exit 1; \
	done
	@echo "✅ All connectors installed!"

install-google-ads:
	cd connectors/ads/google_ads && $(MAKE) install MODE=$(MODE) FORCE_DEPS=$(FORCE_DEPS)

install-microsoft-ads:
	cd connectors/ads/microsoft_ads && $(MAKE) install MODE=$(MODE) FORCE_DEPS=$(FORCE_DEPS)

install-facebook-ads:
	cd connectors/ads/facebook_ads && $(MAKE) install MODE=$(MODE) FORCE_DEPS=$(FORCE_DEPS)

install-files:
	cd connectors/readers/files && $(MAKE) install MODE=$(MODE) FORCE_DEPS=$(FORCE_DEPS)

install-s3:
	cd connectors/readers/s3 && $(MAKE) install MODE=$(MODE) FORCE_DEPS=$(FORCE_DEPS)

install-gcs:
	cd connectors/readers/gcs && $(MAKE) install MODE=$(MODE) FORCE_DEPS=$(FORCE_DEPS)

install-postgres:
	cd connectors/readers/postgres && $(MAKE) install MODE=$(MODE) FORCE_DEPS=$(FORCE_DEPS)

# Generic build command for all packages
build: build-all
build-all:
	@echo "Building distributions for all packages..."
	@echo "Cleaning distribution directory first..."
	@$(MAKE) clean-dist
	@for dir in $(CORE_DIR) $(CONNECTOR_DIRS); do \
		echo "Building $$dir..."; \
		(cd $$dir && $(MAKE) build) || exit 1; \
	done
	@echo "✅ All distributions built successfully!"
	@echo "Distribution files:"
	@find $(DIST_BASE) -name "*.tar.gz" -o -name "*.whl" 2>/dev/null | sort || echo "No distributions found"

# Utility commands
uninstall:
	@echo "Uninstalling all ADaPT packages..."
	pip uninstall -y adapt-google-ads adapt-microsoft-ads adapt-facebook-ads adapt-files adapt-s3 adapt-gcs adapt-postgres \
		adapt-core 2>/dev/null || true
	@echo "✅ All packages uninstalled!"

clean:
	@echo "Cleaning build artifacts..."
	@for dir in $(CORE_DIR) $(CONNECTOR_DIRS); do \
		(cd $$dir && $(MAKE) clean); \
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
	@echo ""
	cd $(CORE_DIR) && $(MAKE) verify
	@echo ""
	@echo "✅ All verifications completed!"

verify-connectors:
	@for dir in $(CONNECTOR_DIRS); do \
		echo ""; \
		(cd $$dir && $(MAKE) verify); \
	done

test:
	python -m pytest adapt-core/tests connectors -q

validate:
	adapt validate --strict examples

# Internal helper commands
_show-packages:
	@echo "Installed packages:"
	@pip list | grep adapt 