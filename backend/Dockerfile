FROM python:3.14-slim

# Microsoft's ODBC driver is needed by the SQL Server connector. The Python
# runtime remains 3.14; credentials are supplied only when the service starts.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && . /etc/os-release \
    && curl -fsSLo /tmp/microsoft-prod.deb "https://packages.microsoft.com/config/debian/${VERSION_ID}/packages-microsoft-prod.deb" \
    && dpkg -i /tmp/microsoft-prod.deb \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && rm /tmp/microsoft-prod.deb && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
RUN python -m pip install --no-cache-dir .
