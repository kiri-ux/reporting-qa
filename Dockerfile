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
# --max-requests recycles a worker after a while, so a slow leak never reaches
# the instance's memory ceiling and takes the service down with it. The jitter
# staggers the two workers so they never recycle together, and a recycle costs
# one cold start on one worker while the other keeps serving.
CMD ["gunicorn","app.main:app","-k","uvicorn.workers.UvicornWorker","-b","0.0.0.0:10000","--timeout","300","--workers","2","--max-requests","800","--max-requests-jitter","200"]
