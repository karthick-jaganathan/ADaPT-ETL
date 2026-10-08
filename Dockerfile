#!/usr/bin/env python
# /*************************************************************************
# * Copyright 2025 Karthick Jaganathan
# *
# * Licensed under the Apache License, Version 2.0 (the "License");
# * you may not use this file except in compliance with the License.
# * You may obtain a copy of the License at
# *
# * https://www.apache.org/licenses/LICENSE-2.0
# *
# * Unless required by applicable law or agreed to in writing, software
# * distributed under the License is distributed on an "AS IS" BASIS,
# * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# * See the License for the specific language governing permissions and
# * limitations under the License.
# **************************************************************************/

# ***********************************
# * StreamWright ETL
# * Local Development Environment
# ***********************************

# Use Python 3.11 slim image as base
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STREAMWRIGHT_CONFIGS=/configs \
    STREAMWRIGHT_OUTPUT_DIR=/data/streamwright_etl

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    make \
    && rm -rf /var/lib/apt/lists/*

# Copy the entire project
COPY . .

# Install streamwright (the `streamwright` and `streamwright-validate` commands) and the source connectors, which bring their own
# dependencies (vendor SDKs, or DuckDB for files, s3, gcs and postgres)
# Alternative 1: Using make (requires make to be installed)
RUN make install MODE=dev && make install-connectors MODE=dev

# Alternative 2: Direct pip installation (uncomment if make is not available)
# RUN pip install -e core \
#     -e connectors/ads/google_ads -e connectors/ads/microsoft_ads -e connectors/ads/facebook_ads \
#     -e connectors/readers/files -e connectors/readers/s3 -e connectors/readers/gcs -e connectors/readers/postgres

# Create a non-root user
RUN useradd --create-home --shell /bin/bash streamwright && \
    chown -R streamwright:streamwright /app
USER streamwright

# Default command
CMD ["streamwright", "--help"]
