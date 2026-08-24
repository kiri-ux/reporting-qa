FROM python:3.12-slim

# poppler-utils gives us pdftotext / pdftoppm / pdfinfo, which the checks rely on
RUN apt-get update && apt-get install -y --no-install-recommends \
      poppler-utils \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PYTHONUNBUFFERED=1
EXPOSE 10000
CMD ["gunicorn","app.main:app","-k","uvicorn.workers.UvicornWorker","-b","0.0.0.0:10000","--timeout","300","--workers","2"]
