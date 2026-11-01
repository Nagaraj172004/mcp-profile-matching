Put extra root certificates here as `*.crt` files (for example a corporate
HTTPS-inspection root). The Docker image adds them to the system trust store,
so pip and the Python HTTP clients keep verifying certificates. Leave the
folder empty otherwise.
