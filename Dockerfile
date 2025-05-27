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
# * ADaPT ETL
# * Local Development Environment
# ***********************************

# Use Python 3.11 slim image as base
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ADAPT_CONFIGS=/configs \
    ADAPT_OUTPUT_DIR=/data/adapt_etl

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    make \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire project
COPY . .

# Install the ADaPT packages in dependency order
# Alternative 1: Using make (requires make to be installed)
RUN make install MODE=dev

# Alternative 2: Direct pip installation (uncomment if make is not available)
# RUN cd adapt/utils && pip install -e . && cd ../.. && \
#     cd adapt/connector && pip install -e . && cd ../.. && \
#     cd adapt/serializer && pip install -e . && cd ../.. && \
#     cd adapt/pipeline && pip install -e . && cd ../..

# Create a non-root user
RUN useradd --create-home --shell /bin/bash adapt && \
    chown -R adapt:adapt /app
USER adapt

# Default command
CMD ["adapt_pipeline", "--help"]
