#!/bin/sh
su - postgres -c "psql -d appcenter -tAc \"select column_name,data_type from information_schema.columns where table_name='app' order by ordinal_position\""
echo "--- rows ---"
su - postgres -c "psql -d appcenter -tAc \"select appname,status,version from app\""
