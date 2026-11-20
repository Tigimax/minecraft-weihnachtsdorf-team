#!/bin/bash
set -e

VOL="/var/lib/pterodactyl/volumes/8651ca23-9889-4e1c-85fc-0fc986727457"
CONTAINER="8651ca23-9889-4e1c-85fc-0fc986727457"

sudo rm -rf "$VOL/botauth_build"
sudo cp -r /tmp/velocity-bot-auth "$VOL/botauth_build"
sudo chown -R 999:987 "$VOL/botauth_build"
sudo chmod -R 777 "$VOL/botauth_build"

docker exec "$CONTAINER" bash -c '
  set -e
  mkdir -p /home/container/botauth_build/classes
  javac -cp "/home/container/velocity.jar" -d /home/container/botauth_build/classes \
    /home/container/botauth_build/src/de/weihnachtsdorf/botauth/LocalBotAuth.java
  cp /home/container/botauth_build/velocity-plugin.json /home/container/botauth_build/classes/
  jar -cf /home/container/botauth_build/WDLocalBotAuth.jar -C /home/container/botauth_build/classes .
'

sudo cp "$VOL/botauth_build/WDLocalBotAuth.jar" "$VOL/plugins/WDLocalBotAuth.jar"
sudo chown 999:987 "$VOL/plugins/WDLocalBotAuth.jar"
sudo chmod 644 "$VOL/plugins/WDLocalBotAuth.jar"
sudo cp "$VOL/plugins/WDLocalBotAuth.jar" /tmp/WDLocalBotAuth.jar
sudo chmod 644 /tmp/WDLocalBotAuth.jar
sudo rm -rf "$VOL/botauth_build"
echo "VELOCITY_BOTAUTH_BUILD_DONE"
