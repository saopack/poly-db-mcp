#!/usr/bin/env bash
set -Eeo pipefail
# TODO swap to -Eeuo pipefail above (after handling all potentially-unset variables)

# usage: file_env VAR [DEFAULT]
#    ie: file_env 'XYZ_DB_PASSWORD' 'example'
# (will allow for "$XYZ_DB_PASSWORD_FILE" to fill in the value of
#  "$XYZ_DB_PASSWORD" from a file, especially for Docker's secrets feature)
file_env() {
        local var="$1"
        local fileVar="${var}_FILE"
        local def="${2:-}"
        if [ "${!var:-}" ] && [ "${!fileVar:-}" ]; then
                printf >&2 'error: both %s and %s are set (but are exclusive)\n' "$var" "$fileVar"
                exit 1
        fi
        local val="$def"
        if [ "${!var:-}" ]; then
                val="${!var}"
        elif [ "${!fileVar:-}" ]; then
                val="$(< "${!fileVar}")"
        fi
        export "$var"="$val"
        unset "$fileVar"
}

# Loads various settinVB that are used elsewhere in the script
# This should be called before any other functions
docker_setup_env() {
        export VB_USER=vastbase
        file_env 'VB_PASSWORD'

        # file_env 'VB_USER' 'vastbase'
        file_env 'VB_DB' "$VB_USER"
        file_env 'POSTGRES_INITDB_ARVB'
        # default authentication method is md5
        : "${VB_HOST_AUTH_METHOD:=md5}"

        declare -g DATABASE_ALREADY_EXISTS
        # look specifically for OG_VERSION, as it is expected in the DB dir
        if [ -s "$PGDATA/PG_VERSION" ]; then
                DATABASE_ALREADY_EXISTS='true'
        fi
}

# append VB_HOST_AUTH_METHOD to pg_hba.conf for "host" connections
vastbase_setup_hba_conf() {
        {
                echo
                echo "host replication vastbase 0.0.0.0/0 md5"
                if [ 'trust' = "$VB_HOST_AUTH_METHOD" ]; then
                        echo '# warning trust is enabled for all connections'
                fi
                echo "host all all 0.0.0.0/0 $VB_HOST_AUTH_METHOD"
        } >> "$PGDATA/pg_hba.conf"
}


# check to see if this file is being run or sourced from another script
_is_sourced() {
        # https://unix.stackexchange.com/a/215279
        [ "${#FUNCNAME[@]}" -ge 2 ] \
                && [ "${FUNCNAME[0]}" = '_is_sourced' ] \
                && [ "${FUNCNAME[1]}" = 'source' ]
}

# used to create initial vastbase directories and if run as root, ensure ownership to the "vastbase" user
docker_create_db_directories() {
        local user; user="$(id -u)"

        mkdir -p "$PGDATA"
        # ignore failure since there are cases where we can't chmod (and PostgreSQL might fail later anyhow - it's picky about permissions of this directory)
        chmod 00700 "$PGDATA" || :

        # ignore failure since it will be fine when using the image provided directory; see also https://github.com/docker-library/vastbase/pull/289
        mkdir -p /var/run/postgresql || :
        chmod 03775 /var/run/postgresql || :

        # Create the transaction log directory before initdb is run so the directory is owned by the correct user
        if [ -n "${POSTGRES_INITDB_WALDIR:-}" ]; then
                mkdir -p "$POSTGRES_INITDB_WALDIR"
                if [ "$user" = '0' ]; then
                        find "$POSTGRES_INITDB_WALDIR" \! -user vastbase -exec chown vastbase '{}' +
                fi
                chmod 700 "$POSTGRES_INITDB_WALDIR"
        fi

        # allow the container to be started with `--user`
        if [ "$user" = '0' ]; then
                find "$PGDATA" \! -user vastbase -exec chown vastbase '{}' +
                find /var/run/postgresql \! -user vastbase -exec chown vastbase '{}' +
                find /home/vastbase/vastbase \! -user vastbase -exec chown vastbase '{}' +
        fi
}

# initialize empty PGDATA directory with new database via 'initdb'
# arguments to `initdb` can be passed via POSTGRES_INITDB_ARGS or as arguments to this function
# `initdb` automatically creates the "vastbase", "template0", and "template1" dbnames
# this is also where the database user is created, specified by `POSTGRES_USER` env
docker_init_database_dir() {
        # "initdb" is particular about the current user existing in "/etc/passwd", so we use "nss_wrapper" to fake that if necessary
        if ! getent passwd "$(id -u)" &> /dev/null && [ -e /usr/lib/libnss_wrapper.so ]; then
                export LD_PRELOAD='/usr/lib/libnss_wrapper.so'
                export NSS_WRAPPER_PASSWD="$(mktemp)"
                export NSS_WRAPPER_GROUP="$(mktemp)"
                echo "postgres:x:$(id -u):$(id -g):PostgreSQL:$PGDATA:/bin/false" > "$NSS_WRAPPER_PASSWD"
                echo "postgres:x:$(id -g):" > "$NSS_WRAPPER_GROUP"
        fi

        if [ -n "$POSTGRES_INITDB_XLOGDIR" ]; then
                set -- --xlogdir "$POSTGRES_INITDB_XLOGDIR" "$@"
        fi

        local initdbCmd='gs_initdb -w $VB_PASSWORD -E UTF8 --locale=en_US.UTF-8  -D $PGDATA'

        if [ -n "$VB_DBCOMPATIBILITY" ]; then
                echo "Specify database compatibility as: "$VB_DBCOMPATIBILITY
                initdbCmd=${initdbCmd}' --dbcompatibility=$VB_DBCOMPATIBILITY'
        else
                echo "Database compatibility uses default values."
        fi
        if [ -n "$VB_NODENAME" ]; then
                initdbCmd=${initdbCmd}' --nodename=$VB_NODENAME'
        else
                initdbCmd=${initdbCmd}' --nodename=vastbase'
        fi

        eval ${initdbCmd}
        # unset/cleanup "nss_wrapper" bits
        if [ "${LD_PRELOAD:-}" = '/usr/lib/libnss_wrapper.so' ]; then
                rm -f "$NSS_WRAPPER_PASSWD" "$NSS_WRAPPER_GROUP"
                unset LD_PRELOAD NSS_WRAPPER_PASSWD NSS_WRAPPER_GROUP
        fi
}

docker_verify_minimum_env() {
        # check password first so we can output the warning before postgres
        # messes it up
        if [[ "$VB_PASSWORD" =~  ^(.{8,}).*$ ]] &&  [[ "$VB_PASSWORD" =~ ^(.*[a-z]+).*$ ]] && [[ "$VB_PASSWORD" =~ ^(.*[A-Z]).*$ ]] &&  [[ "$VB_PASSWORD" =~ ^(.*[0-9]).*$ ]] && [[ "$VB_PASSWORD" =~ ^(.*[#?!@$%^&*-]).*$ ]]; then
                cat >&2 <<-'EOWARN'

                        Message: The supplied VB_PASSWORD is meet requirements.

EOWARN
        else
                 cat >&2 <<-'EOWARN'

                        Error: The supplied VB_PASSWORD is not meet requirements.
                        Please Check if the password contains uppercase, lowercase, numbers, special characters, and password length(8).
                        At least one uppercase, lowercase, numeric, special character.
                        Example: Enmo@123
EOWARN
       exit 1
        fi
        if [ -z "$VB_PASSWORD" ] && [ 'trust' != "$VB_HOST_AUTH_METHOD" ]; then
                # The - option suppresses leading tabs but *not* spaces. :)
                cat >&2 <<-'EOE'
                        Error: Database is uninitialized and superuser password is not specified.
                               You must specify VB_PASSWORD to a non-empty value for the
                               superuser. For example, "-e VB_PASSWORD=password" on "docker run".

                               You may also use "VB_HOST_AUTH_METHOD=trust" to allow all
                               connections without a password. This is *not* recvastbaseended.

EOE
                exit 1
        fi
        if [ 'trust' = "$VB_HOST_AUTH_METHOD" ]; then
                cat >&2 <<-'EOWARN'
                        ********************************************************************************
                        WARNING: VB_HOST_AUTH_METHOD has been set to "trust". This will allow
                                 anyone with access to the vastbase port to access your database without
                                 a password, even if VB_PASSWORD is set.
                                 It is not recvastbaseended to use VB_HOST_AUTH_METHOD=trust. Replace
                                 it with "-e VB_PASSWORD=password" instead to set a password in
                                 "docker run".
                        ********************************************************************************
EOWARN
        fi
}

# usage: docker_process_init_files [file [file [...]]]
#    ie: docker_process_init_files /always-initdb.d/*
# process initializer files, based on file extensions and permissions
docker_process_init_files() {
        # psql here for backwards compatibility "${psql[@]}"
        psql=( docker_process_sql )

        printf '\n'
        local f
        for f; do
                case "$f" in
                        *.sh)
                                # https://github.com/docker-library/vastbase/issues/450#issuecomment-393167936
                                # https://github.com/docker-library/vastbase/pull/452
                                if [ -x "$f" ]; then
                                        printf '%s: running %s\n' "$0" "$f"
                                        "$f"
                                else
                                        printf '%s: sourcing %s\n' "$0" "$f"
                                        . "$f"
                                fi
                                ;;
                        *.sql)     printf '%s: running %s\n' "$0" "$f"; docker_process_sql -f "$f"; printf '\n' ;;
                        *.sql.gz)  printf '%s: running %s\n' "$0" "$f"; gunzip -c "$f" | docker_process_sql; printf '\n' ;;
                        *.sql.xz)  printf '%s: running %s\n' "$0" "$f"; xzcat "$f" | docker_process_sql; printf '\n' ;;
                        *.sql.zst) printf '%s: running %s\n' "$0" "$f"; zstd -dc "$f" | docker_process_sql; printf '\n' ;;
                        *)         printf '%s: ignoring %s\n' "$0" "$f" ;;
                esac
                printf '\n'
        done
}

# Execute sql script, passed via stdin (or -f flag of pqsl)
# usage: docker_process_sql [psql-cli-args]
#    ie: docker_process_sql --dbname=mydb <<<'INSERT ...'
#    ie: docker_process_sql -f my-file.sql
#    ie: docker_process_sql <my-file.sql
docker_process_sql() {
        local query_runner=( vsql -v ON_ERROR_STOP=1 )
        if [ -n "$VB_DB" ]; then
                query_runner+=( --dbname "$VB_DB" )
        fi

        PGHOST= PGHOSTADDR= "${query_runner[@]}" "$@"
}

# create initial database
# uses environment variables for input: VB_DB
docker_setup_db() {
        local dbAlreadyExists
        dbAlreadyExists="$(
                VB_DB= docker_process_sql --dbname vastbase --set db="$VB_DB" --tuples-only <<'EOSQL'
                        SELECT 1 FROM pg_database WHERE datname = :'db' ;
EOSQL
        )"
        if [ -z "$dbAlreadyExists" ]; then
                VB_DB= docker_process_sql --dbname vastbase --set db="$VB_DB" <<'EOSQL'
                        CREATE DATABASE :"db" ;
EOSQL
                printf '\n'
        fi
}

docker_setup_user() {
        if [ -n "$VB_USERNAME" ]; then
                user_privilege=login
                if [ -n "$USER_PRIVILEGE" ]; then
                        user_privilege=$USER_PRIVILEGE
                fi
                VB_DB= docker_process_sql --dbname postgres --set db="$VB_DB" --set passwd="$VB_PASSWORD" --set user="$VB_USERNAME" --set privilege="$user_privilege" <<-'EOSQL'
                        create user :"user" with :"privilege" password :"passwd" ;
EOSQL
        else
                echo " default user is vastbase"
        fi
}

pg_setup_postgresql_conf() {

        {
                echo "listen_addresses = '*'"
                echo "shared_preload_libraries='pg_stat_statements'"
                if [ -n "$OTHER_PG_CONF" ]; then
                    echo -e "$OTHER_PG_CONF"
                fi
                echo

        } >> "$PGDATA/postgresql.conf"
}

pg_setup_mot_conf() {
         echo "enable_numa = false" >> "$PGDATA/mot.conf"
}


# start socket-only postgresql server for setting up or running scripts
# all arguments will be passed along as arguments to `vastbase` (via vb_ctl)
docker_temp_server_start() {
        if [ "$1" = 'vastbase' ]; then
                shift
        fi

        # internal start of server in order to allow setup using psql client
        # does not listen on external TCP/IP and waits until start finishes
        set -- "$@" -c listen_addresses='localhost' -p "${PGPORT:-5432}"

        # unset NOTIFY_SOCKET so the temporary server doesn't prematurely notify
        # any process supervisor.
        NOTIFY_SOCKET= \
        PGUSER="${PGUSER:-$vastbase}" \
        vb_ctl -D "$PGDATA" \
                -o "$(printf '%q ' "$@")" \
                -w start
}

# stop postgresql server after done setting up user and running scripts
docker_temp_server_stop() {
        PGUSER="${PGUSER:-vastbase}" \
        vb_ctl -D "$PGDATA" -m fast -w stop
}

# check arguments for an option that would cause vastbase to stop
# return true if there is one
_pg_want_help() {
        local arg
        for arg; do
                case "$arg" in
                        # vastbase --help | grep 'then exit'
                        # leaving out -C on purpose since it always fails and is unhelpful:
                        # vastbase: could not access the server configuration file "/var/lib/postgresql/data/postgresql.conf": No such file or directory
                        -'?'|--help|--describe-config|-V|--version)
                                return 0
                                ;;
                esac
        done
        return 1
}

_main() {
        # if first arg looks like a flag, assume we want to run vastbase server
        if [ "${1:0:1}" = '-' ]; then
                set -- vastbase "$@"
        fi

        if [ "$1" = 'vastbase' ] && ! _pg_want_help "$@"; then
                docker_setup_env
                # setup data directories and permissions (when run as root)
                docker_create_db_directories
                if [ "$(id -u)" = '0' ]; then
                        # then restart script as vastbase user
                        exec gosu vastbase "$BASH_SOURCE" "$@"
                fi

                # only run initialization on an empty data directory
                if [ -z "$DATABASE_ALREADY_EXISTS" ]; then
                        # check dir permissions to reduce likelihood of half-initialized database
                        ls /docker-entrypoint-initdb.d/ > /dev/null                        
                        docker_verify_minimum_env
                        docker_init_database_dir
                        # apply custom postgresql.conf and pg_hba.conf if mounted
                        # (copy first, then append critical settings so they aren't lost)
                        if [ -f /docker-entrypoint-initdb.d/postgresql.conf ]; then
                                cp -f /docker-entrypoint-initdb.d/postgresql.conf "$PGDATA/postgresql.conf"
                        fi
                        if [ -f /docker-entrypoint-initdb.d/pg_hba.conf ]; then
                                cp -f /docker-entrypoint-initdb.d/pg_hba.conf "$PGDATA/pg_hba.conf"
                        fi
                        vastbase_setup_hba_conf
                        pg_setup_postgresql_conf
                        pg_setup_mot_conf
                        # PGPASSWORD is required for psql when authentication is required for 'local' connections via pg_hba.conf and is otherwise harmless
                        # e.g. when '--auth=md5' or '--auth-local=md5' is used in POSTGRES_INITDB_ARGS
                        export PGPASSWORD="${PGPASSWORD:-$POSTGRES_PASSWORD}"
                        docker_temp_server_start "$@"

                        docker_setup_db
                        docker_setup_user
                        docker_process_init_files /docker-entrypoint-initdb.d/*

                        docker_temp_server_stop
                        unset PGPASSWORD

                        cat <<'EOM'

                                PostgreSQL init process complete; ready for start up.

EOM
                else
                        cat <<'EOM'

                                PostgreSQL Database directory appears to contain a database; Skipping initialization

EOM
                fi
        fi

        exec "$@"
}

if ! _is_sourced; then
        _main "$@"
fi
