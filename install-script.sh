#!/bin/sh

echo "moving files, super user permissions needed:
"

sudo cp couchmpv.py /bin
sudo cp mptv.desktop /usr/share/applications
sudo chmod +x /usr/share/applications/mptv.desktop
sudo chmod +x /bin/couchmpv.py

echo "
Finished!"
