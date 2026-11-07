FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install --timeout 60 --retries 8 -r requirements.txt
RUN useradd --create-home oncoaid
COPY . .
USER oncoaid
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
