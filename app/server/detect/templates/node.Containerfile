FROM {{node}}

WORKDIR /app

COPY package*.json ./
RUN {{install}}

COPY . /app
{{build}}

ENV NODE_ENV=production \
    PORT=8000

CMD {{cmd}}
