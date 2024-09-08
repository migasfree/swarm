#!/bin/sh

timeout 1 curl --fail --silent --head --request GET http://localhost:5555 > /dev/null
if [ $? -eq 0 ]
then
    if ! [ -f /var/tmp/healthy ]
    then
        touch /var/tmp/healthy
        send_message ""
        # Important: Reconfigure in the background after 3 seconds; this container must be healthy first.
        reload_proxy 3
    fi  
else
    rm /var/tmp/healthy || :
    send_message "Service Unavailable"
    exit 1  
fi

exit 0