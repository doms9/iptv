#!/usr/bin/env bash

UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0"
MAX_JOBS=10
BASE_FILE="./M3U8/base.m3u8"
README="./readme.md"

[[ ! -f $BASE_FILE ]] && {
    echo "$BASE_FILE does not exist" >&2
    exit 1
}

shopt -s nocasematch

STATUSLOG=$(mktemp)

get_status() {
    local url="$1"
    local channel="$2"
    local index="$3"
    local total="$4"
    local referer="$5"

    local chnl_info response rc IFS status_code content_type index_width

    [[ $url != http* ]] && return

    printf -v chnl_info "%s (%s)\n" "$channel" "$url"

    response=$(
        curl -skL \
            -A "$UA" \
            -H "Accept: */*" \
            -H "Accept-Language: en-US,en;q=0.9" \
            -H "Connection: keep-alive" \
            -o /dev/null \
            -e "$referer" \
            --compressed \
            --max-time 10 \
            -w "%{http_code}|%{content_type}" \
            "$url" 2>&1
    )

    rc=$?

    IFS="|" read -r status_code content_type <<<"$response"

    index_width=${#total}

    if ((rc != 0)); then
        if [[ $status_code == 2* && $rc == 28 ]]; then
            printf "[%${index_width}d/%d]\t%b\t%s" \
                "$index" "$total" "\u2714\ufe0f" "$chnl_info"

        else
            printf "[%${index_width}d/%d]\t%b\t%s" \
                "$index" "$total" "\U274C" "$chnl_info"

            printf "%s\t%s\tcURL Error (%s)\n" \
                "$url" "$channel" "$rc" >>"$STATUSLOG"
        fi

    elif [[ $status_code != 2* ]]; then
        printf "[%${index_width}d/%d]\t%b\t%s" \
            "$index" "$total" "\U274C" "$chnl_info"

        printf "%s\t%s\tHTTP Error (%s)\n" \
            "$url" "$channel" "$status_code" >>"$STATUSLOG"

    else
        case "$content_type" in

        application/vnd.apple.mpegurl* | \
            application/x-mpegURL* | \
            application/octet-stream* | \
            video/mpeg* | \
            video/mp2t* | \
            text/plain*)

            printf "[%${index_width}d/%d]\t%b\t%s" \
                "$index" "$total" "\u2714\ufe0f" "$chnl_info"
            ;;

        text/html* | *)

            printf "[%${index_width}d/%d]\t%b\t%s" \
                "$index" "$total" "\U274C" "$chnl_info"

            printf "%s\t%s\tInvalid Source (%s)\n" \
                "$url" "$channel" "$status_code" >>"$STATUSLOG"
            ;;
        esac
    fi
}

check_links() {
    local total_urls=$1

    local channel_num=1
    local name=""

    local IFS line referer

    printf "Checking %d links from %s\n\n" "$total_urls" "$BASE_FILE"

    while IFS= read -r line; do
        line=${line//$'\r'/}

        if [[ $line == \#EXTINF* ]]; then
            name=$(sed -n 's/.*tvg-name="\([^"]*\)".*/\1/p' <<<"$line")

            [[ -z $name ]] && name="Channel $channel_num"

            referer="https://google.com"

        elif [[ $line == \#EXTVLCOPT:http-referrer=* ]]; then
            referer=${line#*=}

        elif [[ $line =~ ^https?:// ]]; then
            while (($(jobs -rp | wc -l) >= MAX_JOBS)); do wait -n; done

            get_status "$line" "$name" "$channel_num" "$total_urls" "$referer" &

            ((channel_num++))
        fi

    done <"$BASE_FILE"

    wait
    echo -e "\nDone."
}

write_readme() {
    local total_urls=$1

    local base="https://s.id/d9Base"
    local live="https://s.id/d9Live"
    local combined="https://s.id/d9M3U8"
    local kodi="https://s.id/d9Kod"
    local epg="https://s.id/d9sEPG"
    local commits="https://github.com/doms9/iptv/commits/default"
    local license="https://github.com/doms9/iptv/blob/default/LICENSE"

    local datefmt="%Y-%m-%d %H:%M %Z"
    local TZ IFS url channel error failed passed

    failed=$(grep -cE '(Error|Invalid)' "$STATUSLOG")
    passed=$((total_urls - failed))

    {
        echo "<div align='center'>"
        printf "<h1>%b IPTV</h1>\n\n" "\U1F4FA"

        printf "[![update freq.](%s)](%s)\n" "https://img.shields.io/badge/updates-hourly-ac99e0" "$base"
        printf "[![commits](%s)](%s)\n" "https://img.shields.io/github/commit-activity/w/doms9/iptv" "$commits"
        printf "[![license](%s)](%s)\n" "https://img.shields.io/github/license/doms9/iptv?logoColor=86b58c" "$license"
        printf "![python](%s)\n" "https://img.shields.io/badge/Python-4584b6?logo=python&logoColor=fff"

        TZ="UTC" printf "\n## [Base](M3U8/base.m3u8) Log @ %($datefmt)T\n" -1

        printf "\n<h3>"
        printf "%b Working Streams: %d" "\U2705" "$passed"
        printf "<br>"
        printf "%b Dead Streams: %d" "\U274C" "$failed"
        echo "</h3>"

        if ((failed > 0)); then
            echo "<table>"
            printf "<tr><th>Channel</th>"
            printf "<th>Error (Code)</th></tr>\n"

            while IFS=$'\t' read -r url channel error; do
                printf "<tr><td>"
                printf "<a href='%s'>%s</a></td>" "$url" "$channel"
                printf "<td>%s</td></tr>\n" "$error"
            done < <(sort -V -t $'\t' -k 2,2 -u -f "$STATUSLOG")

            echo "</table>"
        fi

        echo -e "</div>\n\n---"

        echo "#### Base Channels"
        # shellcheck disable=SC2016
        printf '```\n%s\n```\n\n' "$base"

        echo "#### Live Events"
        # shellcheck disable=SC2016
        printf '```\n%s\n```\n\n' "$live"

        echo "#### Combined (Base Channels + Live Events)"
        # shellcheck disable=SC2016
        printf '```\n%s\n```\n\n' "$combined"

        echo "#### Kodi (Base Channels + Live Events)"
        # shellcheck disable=SC2016
        printf '```\n%s\n```\n\n' "$kodi"

        echo "#### EPG"
        # shellcheck disable=SC2016
        printf '```\n%s\n```\n\n' "$epg"

        echo "---"
        echo "#### Mirrors"
        echo -n "[GitHub](https://github.com/doms9/iptv) | "
        echo -e "[GitLab](https://gitlab.com/doms9/iptv) |"
        echo -e "[Forgejo](https://forgejo.mxnticek.eu/doms/iptv)\n"
        echo "---"
        echo "#### Legal Disclaimer"
        echo "This repository lists publicly accessible IPTV streams as found on the internet at the time of checking."
        echo "No video or audio content is hosted in this repository. These links may point to copyrighted material owned by third parties;"
        echo "they are provided **solely for educational and research purposes.**"
        echo "The author does not endorse, promote, or encourage illegal streaming or copyright infringement."
        echo "End users are solely responsible for ensuring they comply with all applicable laws in their jurisdiction before using any link in this repository."
        echo "If you are a rights holder and wish for a link to be removed, please open an issue."

    } >"$README"
}

total_urls=$(grep -cE '^https?://' "$BASE_FILE")

check_links "$total_urls"
write_readme "$total_urls"
rm "$STATUSLOG"
