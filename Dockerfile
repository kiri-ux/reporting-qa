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
# TWO WORKERS. THE MEMORY CEILING IS SHARED AND IT IS THE BINDING ONE.
#
# I raised this to three to give a cheap request somewhere to land while the
# other two were busy, because Render was calling the instance failed when
# /healthz did not answer inside five seconds. Then Render killed the instance
# for exceeding its memory limit, which is the same three workers seen from the
# other side: the 2,048 MB is for the WHOLE INSTANCE, not per process, and a
# worker was measured holding 348 MB with the order parse peaking a couple of
# hundred more on top of one of them. Three of those plus a page build each is
# how you reach the ceiling.
#
# The health-check argument is much weaker now anyway - /healthz is answered on
# the event loop and does nothing, so it does not queue behind a page. Two
# workers, and memory stops being the thing that restarts the service.
#
# If three are wanted, the instance has to be bigger. That is a plan change,
# not a flag.
#
# --max-requests recycles a worker so a slow leak never reaches the ceiling.
# Halved along with this: at 800 a leaking worker had four times as long to
# grow, and trimming twice as often costs one cold start on one worker while
# the other keeps serving. The jitter staggers them so they never recycle
# together.
CMD ["gunicorn","app.main:app","-k","uvicorn.workers.UvicornWorker","-b","0.0.0.0:10000","--timeout","300","--workers","2","--max-requests","400","--max-requests-jitter","120"]
