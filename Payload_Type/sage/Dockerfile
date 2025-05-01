FROM itsafeaturemythic/mythic_go_base:latest

WORKDIR /Mythic

COPY [".", "."]

## Install dependencies
RUN apt update
RUN apt install -y python3 python3-pip git
RUN pip3 install uv

# Build the container
WORKDIR /Mythic/container
RUN go build -o /sage

CMD ["/sage"]