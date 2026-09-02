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
# staggers the workers so they never recycle together, and a recycle costs one
# cold start on one worker while the others keep serving.
#
# THREE WORKERS, NOT TWO, FOR AVAILABILITY RATHER THAN THROUGHPUT.
#
# The box has half a core, so a third worker buys no extra work per second -
# what it buys is somewhere for a cheap request to land while the other two are
# busy. Render asks for /healthz, waits five seconds and calls the instance
# failed, and with two workers it only takes two slow page builds at the same
# moment - two people on the board, or one person and the order re-read - for
# there to be nobody free to say yes. Nothing was ever down.
#
# Measured headroom: the instance reports a 2,048 MB limit and a worker sits
# around 200 MB, with the order parse peaking about 233 MB on top of one of
# them. Three fits with room to spare.
CMD ["gunicorn","app.main:app","-k","uvicorn.workers.UvicornWorker","-b","0.0.0.0:10000","--timeout","300","--workers","3","--max-requests","800","--max-requests-jitter","200"]
