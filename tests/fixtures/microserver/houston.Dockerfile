# Owned local tst fixture. Supply only the reviewed public helpers and fixed DEB.
FROM docker.io/prom/alertmanager:v0.34.1@sha256:84967b9b7ba45e38a9278d3e594305f43d4993c310df3905b51138b816c365f3 AS alertmanager
FROM docker.io/library/python:3.13-slim-trixie@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc
RUN apt-get update && apt-get install -y --no-install-recommends python3-dbus python3-gi python3-requests dbus-daemon dbus-system-bus-common && rm -rf /var/lib/apt/lists/*
COPY cockpit-alerts.deb /fixture.deb
RUN echo '2f94e45ff6b63e79f94ea9c8724b5f88b3ac4f2216a6cf353603174cc0b0511f  /fixture.deb' | sha256sum --check && dpkg-deb --extract /fixture.deb / && rm /fixture.deb && useradd --system --no-create-home --shell /usr/sbin/nologin microserver-alerts && useradd --system --no-create-home --shell /usr/sbin/nologin nasadmin
COPY --from=alertmanager /bin/alertmanager /bin/amtool /usr/local/bin/
COPY microserver-houston-dbus.py microserver-houston-webhook.py /usr/local/libexec/
COPY policy.conf /etc/dbus-1/system.d/org._45drives.Houston.conf
COPY alertmanager.yaml /fixture/alertmanager.yaml
COPY houston_native.py /fixture/houston_native.py
ENTRYPOINT ["/usr/bin/python3", "/fixture/houston_native.py"]
