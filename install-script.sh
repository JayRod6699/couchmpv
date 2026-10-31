#!/bin/sh

echo "Installing CouchMPV..."
echo "Superuser permissions required."
echo

sudo install -m 755 couchmpv.py /usr/local/bin/couchmpv.py
sudo install -Dm 644 couchmpv.desktop /usr/share/applications/couchmpv.desktop

echo
echo "CouchMPV installation finished!"
