FROM limit-usage:local

RUN pip install --no-cache-dir 'PyYAML>=6'
COPY catalog ./catalog
COPY app ./app

