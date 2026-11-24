FROM python:3.12-slim

RUN useradd --create-home --uid 1000 app
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY knowledge ./knowledge
COPY inputs ./inputs
RUN pip install --no-cache-dir . && chown -R app:app /app
USER app

# Stdlib only. Listening on 0.0.0.0 inside the container requires ORKI_TOKEN (the panel refuses to start otherwise).
ENV ORKI_HOST=0.0.0.0 PYTHONUNBUFFERED=1
EXPOSE 8765
CMD ["python", "-m", "orkiestrator", "serve", "--no-browser"]
