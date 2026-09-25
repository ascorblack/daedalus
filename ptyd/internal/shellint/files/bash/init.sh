# Shell integration for bash. The terminal daemon starts bash with this file as its --init-file,
# because an interactive bash reads nothing else that the daemon could extend without touching the
# user's own files. So it first reads the startup files bash would have read itself, then adds the
# marks: OSC 133 A at the start of the prompt and B at its end, C when a command starts, D with the
# exit status when it ends, OSC 633 E with the command line, and OSC 7 with the directory.
#
# Every mark carries k=<nonce>, a random value per launch. The daemon ignores a mark without it, so
# replaying a recorded session, or a nested shell over ssh, cannot report "finished, exit 0". The
# nonce is taken out of the environment at once, so programs started from this shell never see it.
#
# What the user set up is extended, never replaced: PROMPT_COMMAND keeps its entries (it gains one
# before them and one after), PS0 and PS1 keep their text, a DEBUG trap is chained, and bash-preexec,
# when the user loads it, is used instead of any of those.

if [[ -n "${__dsi_loaded:-}" ]]; then
    return 0
fi
__dsi_loaded=1

__dsi_nonce=${DAEDALUS_SI_NONCE:-}
__dsi_login=${DAEDALUS_SHELL_LOGIN:-}
# The tests force the DEBUG-trap way, which only a bash older than 4.4 would otherwise take.
__dsi_mode=${DAEDALUS_SI_BASH_MODE:-}
unset DAEDALUS_SI_NONCE DAEDALUS_SHELL_LOGIN DAEDALUS_SI_BASH_MODE

# The startup files. --init-file replaces ~/.bashrc and is not read by a login shell, so a login
# shell's files are read here in bash's own order; when none of the personal ones exists, ~/.bashrc
# is read instead, as a login shell usually reaches it through ~/.profile anyway.
if [[ -n "$__dsi_login" ]]; then
    if [[ -r /etc/profile ]]; then
        . /etc/profile
    fi
    if [[ -r ~/.bash_profile ]]; then
        . ~/.bash_profile
    elif [[ -r ~/.bash_login ]]; then
        . ~/.bash_login
    elif [[ -r ~/.profile ]]; then
        . ~/.profile
    elif [[ -r ~/.bashrc ]]; then
        . ~/.bashrc
    fi
elif [[ -r ~/.bashrc ]]; then
    . ~/.bashrc
fi

if [[ -n "$__dsi_nonce" ]]; then

__dsi_a=$'\e]133;A;k='"$__dsi_nonce"$'\a'
__dsi_b=$'\e]133;B;k='"$__dsi_nonce"$'\a'
__dsi_c=$'\e]133;C;k='"$__dsi_nonce"$'\a'
__dsi_first=1
__dsi_ready=
__dsi_cwd_seen=
__dsi_cwd_url=

# __dsi_escape sets __dsi_escaped to $1 as an OSC 633 value: a backslash doubled, and ';' and every
# control character as \xNN, so the command line can neither end the sequence nor split its fields.
# It is cut at 4096 characters first; the daemon keeps no more than that of a command line.
__dsi_escape() {
    local s=${1:0:4096} out= c i
    s=${s//\\/\\\\}
    s=${s//;/\\x3b}
    if [[ "$s" == *[[:cntrl:]]* ]]; then
        local LC_ALL=C
        for ((i = 0; i < ${#s}; i++)); do
            c=${s:i:1}
            if [[ "$c" == [[:cntrl:]] ]]; then
                printf -v c '\\x%02x' "'$c"
            fi
            out+=$c
        done
        s=$out
    fi
    __dsi_escaped=$s
}

# __dsi_cwd prints OSC 7 for the working directory, percent-encoded byte by byte. The encoding is
# redone only when the directory changed.
__dsi_cwd() {
    if [[ "$PWD" != "$__dsi_cwd_seen" ]]; then
        local LC_ALL=C s=$PWD out= c i
        for ((i = 0; i < ${#s}; i++)); do
            c=${s:i:1}
            case "$c" in
                [a-zA-Z0-9/._~-]) out+=$c ;;
                *) printf -v c '%%%02X' "'$c"; out+=$c ;;
            esac
        done
        __dsi_cwd_seen=$PWD
        __dsi_cwd_url=$out
    fi
    builtin printf '\e]7;file://%s%s\a' "${HOSTNAME:-}" "$__dsi_cwd_url"
}

# __dsi_started prints E with the command line (when it is known) and C.
__dsi_started() {
    if [[ -n "${1+set}" ]]; then
        __dsi_escape "$1"
        builtin printf '\e]633;E;%s;k=%s\a' "$__dsi_escaped" "$__dsi_nonce"
    fi
    builtin printf '%s' "$__dsi_c"
}

# __dsi_histline sets __dsi_line to the command bash is about to run, from the history, and fails
# when the history does not hold it. The number of the entry is checked against the one the prompt
# promised (HISTCMD): a command that did not enter the history — a leading space under ignorespace,
# or history switched off — is then reported without its text instead of as the command before it.
# A repeat dropped by ignoredups alone is the entry before, so that one is used.
__dsi_histline() {
    local entry num
    entry=$(HISTTIMEFORMAT= builtin history 1)
    entry=${entry#"${entry%%[![:space:]]*}"}
    num=${entry%%[!0-9]*}
    [[ -n "$num" ]] || return 1
    __dsi_line=${entry:${#num}+2}
    if [[ "$num" == "${__dsi_next:-}" ]]; then
        return 0
    fi
    case ":${HISTCONTROL:-}:" in
        *:ignorespace:* | *:ignoreboth:*) return 1 ;;
        *:ignoredups:*) [[ "$num" == "$((${__dsi_next:-0} - 1))" ]] && return 0 ;;
    esac
    return 1
}

# The command start, three ways. bash-preexec, when loaded, owns the DEBUG trap and PS0 and hands
# over the command line; bash 4.4 and later expand PS0 after reading a command and before running
# it; older bash has only the DEBUG trap, which is chained after the user's own.
if [[ -z "$__dsi_mode" && -n "${bash_preexec_imported:-}${__bp_imported:-}" ]]; then
    __dsi_mode=preexec
    __dsi_bp_preexec() { __dsi_started "$1"; }
elif [[ -z "$__dsi_mode" ]] && ((BASH_VERSINFO[0] > 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] >= 4))); then
    __dsi_mode=ps0
    # PS0 is expanded in a subshell of its own, so this can print but cannot set anything.
    __dsi_ps0() {
        if __dsi_histline; then
            __dsi_started "$__dsi_line"
        else
            __dsi_started
        fi
    }
    PS0='$(__dsi_ps0)'"${PS0:-}"
else
    __dsi_mode=debug
    __dsi_user_debug=$(trap -p DEBUG)
    __dsi_user_debug=${__dsi_user_debug#trap -- }
    __dsi_user_debug=${__dsi_user_debug% DEBUG}
    eval "__dsi_user_debug=${__dsi_user_debug:-''}"
    # The trap also fires for the prompt commands. After an empty Enter the first thing that runs is
    # __dsi_precmd, which is no command of the user's.
    __dsi_debug() {
        if [[ "$BASH_COMMAND" == __dsi_precmd* ]]; then
            __dsi_ready=
        elif [[ -n "$__dsi_ready" && -z "${COMP_LINE:-}" ]]; then
            __dsi_ready=
            if __dsi_histline; then
                __dsi_started "$__dsi_line"
            else
                __dsi_started
            fi
        fi
    }
    trap '__dsi_debug; eval "${__dsi_user_debug}"' DEBUG
fi

# __dsi_precmd runs first after a command: it reports how the command ended and where the shell is
# now, and hands the status on unchanged to whatever runs after it. The first prompt has no command
# before it. A D after a line that ran nothing (an empty Enter) is ignored by the daemon, which only
# closes a command it saw start.
__dsi_precmd() {
    local status=$?
    if [[ -z "$__dsi_first" ]]; then
        builtin printf '\e]133;D;%s;k=%s\a' "$status" "$__dsi_nonce"
    fi
    __dsi_first=
    __dsi_cwd
    return $status
}

# __dsi_prompt runs last, after the user's own prompt commands have built PS1 (a prompt framework
# rebuilds it at every prompt): A goes out now, where the prompt is about to be drawn, and B is
# appended to PS1 when it is not already there, inside \[ \] so readline counts it as no width.
__dsi_prompt() {
    local status=$?
    if [[ "$PS1" != *"$__dsi_b"* ]]; then
        PS1="${PS1}\\[${__dsi_b}\\]"
    fi
    # HISTCMD here is the number the next command will get. (\! expanded with @P is not: inside
    # the prompt commands it lags one behind once the history has entries.) Before 4.4 — the bash
    # 3.2 every Mac still ships — HISTCMD inside the prompt commands is always 1, so every command
    # after the first was reported without its text; there the number is counted from the history.
    if [[ -n "${HISTCMD:-}" ]] && ((BASH_VERSINFO[0] > 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] >= 4))); then
        __dsi_next=$HISTCMD
    else
        __dsi_next=$(HISTTIMEFORMAT= builtin history 1)
        __dsi_next=${__dsi_next#"${__dsi_next%%[![:space:]]*}"}
        __dsi_next=${__dsi_next%%[!0-9]*}
        __dsi_next=$((${__dsi_next:-0} + 1))
    fi
    builtin printf '%s' "$__dsi_a"
    __dsi_ready=1
    return $status
}

if [[ "$__dsi_mode" == preexec ]]; then
    precmd_functions=(__dsi_precmd "${precmd_functions[@]}" __dsi_prompt)
    preexec_functions+=(__dsi_bp_preexec)
elif [[ "$(declare -p PROMPT_COMMAND 2>/dev/null)" == "declare -a"* ]]; then
    PROMPT_COMMAND=(__dsi_precmd "${PROMPT_COMMAND[@]}" __dsi_prompt)
else
    PROMPT_COMMAND=$'__dsi_precmd\n'"${PROMPT_COMMAND:-}"$'\n__dsi_prompt'
fi

fi
