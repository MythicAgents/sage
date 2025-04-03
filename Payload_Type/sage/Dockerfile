FROM itsafeaturemythic/mythic_go_base:latest

WORKDIR /Mythic

COPY [".", "."]

# Build the container
WORKDIR /Mythic/container
RUN go build -o /sage

CMD ["/sage"]