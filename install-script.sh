#!/bin/sh

echo "moving files, super user permissions needed:
"

sudo cp couchmpv.py /bin
sudo cp couchmpv.desktop /usr/share/applications
sudo chmod +x /usr/share/applications/couchmpv.desktop
sudo chmod +x /bin/couchmpv.py

echo "
Finished!"
