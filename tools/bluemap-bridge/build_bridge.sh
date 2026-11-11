#!/bin/bash
set -e

sudo rm -rf /var/lib/pterodactyl/volumes/a173d02e-4efc-4f53-bc4f-4dd42f99c6f8/bridge_build
sudo cp -r /tmp/bluemap-bridge /var/lib/pterodactyl/volumes/a173d02e-4efc-4f53-bc4f-4dd42f99c6f8/bridge_build
sudo chown -R 999:987 /var/lib/pterodactyl/volumes/a173d02e-4efc-4f53-bc4f-4dd42f99c6f8/bridge_build
sudo chmod -R 777 /var/lib/pterodactyl/volumes/a173d02e-4efc-4f53-bc4f-4dd42f99c6f8/bridge_build

docker exec a173d02e-4efc-4f53-bc4f-4dd42f99c6f8 bash -c '
  set -e
  CP="/home/container/libraries/org/purpurmc/purpur/purpur-api/1.21.4-R0.1-SNAPSHOT/purpur-api-1.21.4-R0.1-SNAPSHOT.jar"
  for j in $(find /home/container/libraries -name "*.jar"); do
    CP="$CP:$j"
  done
  for p in /home/container/plugins/*.jar; do
    CP="$CP:$p"
  done
  mkdir -p /home/container/bridge_build/classes
  mkdir -p /home/container/bridge_build/stubs_classes
  javac -d /home/container/bridge_build/stubs_classes /home/container/bridge_build/stubs/org/jetbrains/annotations/*.java
  CP="$CP:/home/container/bridge_build/stubs_classes"
  javac -cp "$CP" -d /home/container/bridge_build/classes /home/container/bridge_build/src/de/weihnachtsdorf/bmbridge/BlueMapLiveBridge.java
  cp /home/container/bridge_build/plugin.yml /home/container/bridge_build/classes/
  jar -cf /home/container/bridge_build/BlueMapLiveBridge.jar -C /home/container/bridge_build/classes .
'

sudo cp /var/lib/pterodactyl/volumes/a173d02e-4efc-4f53-bc4f-4dd42f99c6f8/bridge_build/BlueMapLiveBridge.jar /var/lib/pterodactyl/volumes/a173d02e-4efc-4f53-bc4f-4dd42f99c6f8/plugins/BlueMapLiveBridge.jar
sudo chown 999:987 /var/lib/pterodactyl/volumes/a173d02e-4efc-4f53-bc4f-4dd42f99c6f8/plugins/BlueMapLiveBridge.jar
sudo chmod 644 /var/lib/pterodactyl/volumes/a173d02e-4efc-4f53-bc4f-4dd42f99c6f8/plugins/BlueMapLiveBridge.jar

sudo cp /var/lib/pterodactyl/volumes/a173d02e-4efc-4f53-bc4f-4dd42f99c6f8/plugins/BlueMapLiveBridge.jar /tmp/BlueMapLiveBridge.jar
sudo chmod 644 /tmp/BlueMapLiveBridge.jar
echo "BUILD_DONE"
