FROM rabbitmq:4.4.1@sha256:183272aa2ff82718cabeef343de8d9a900931ea65fda4e563fed2a6d9efde578
# The base ships a service manager the broker never runs; the image scan counts every binary in the image.
RUN rm /usr/bin/pebble
