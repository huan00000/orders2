FROM debian:bookworm-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
    cmake g++ make libcurl4-openssl-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY CMakeLists.txt main.cpp 1.cpp ./
RUN cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build build --parallel

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcurl4 libssl3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/build/orders2 /usr/local/bin/orders2
WORKDIR /app
ENTRYPOINT ["/usr/local/bin/orders2"]
