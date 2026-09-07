FROM node:20-slim

WORKDIR /app

# Install git and ca-certificates if needed for npm packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
    wget \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY package*.json ./
RUN npm install

COPY . .

EXPOSE 5426
CMD ["npm", "start"]
