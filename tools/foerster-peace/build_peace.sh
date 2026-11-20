#!/bin/bash
set -e

VOL="/var/lib/pterodactyl/volumes/a173d02e-4efc-4f53-bc4f-4dd42f99c6f8"
CID="a173d02e-4efc-4f53-bc4f-4dd42f99c6f8"

sudo rm -rf "$VOL/peace_build"
sudo cp -r /tmp/foerster-peace "$VOL/peace_build"
sudo chown -R 999:987 "$VOL/peace_build"
sudo chmod -R 777 "$VOL/peace_build"

docker exec "$CID" bash -c '
  set -e
  CP=""
  for j in $(find /home/container/libraries -name "*.jar"); do
    CP="${CP:+$CP:}$j"
  done
  mkdir -p /home/container/peace_build/classes
  javac -cp "$CP" -d /home/container/peace_build/classes /home/container/peace_build/src/de/weihnachtsdorf/foersterpeace/FoersterPeacePlugin.java
  cp /home/container/peace_build/plugin.yml /home/container/peace_build/classes/
  jar -cf /home/container/peace_build/FoersterPeace.jar -C /home/container/peace_build/classes .
'

sudo install -m 0644 -o 999 -g 987 "$VOL/peace_build/FoersterPeace.jar" "$VOL/plugins/FoersterPeace.jar"
sudo rm -rf "$VOL/peace_build"
echo "PEACE_BUILD_DONE"
