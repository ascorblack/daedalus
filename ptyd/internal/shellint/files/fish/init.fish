# Shell integration for fish. The terminal daemon starts fish with --init-command sourcing this
# file, which fish runs after the user's configuration and before the first prompt, so nothing of
# the user's is replaced or even read differently. It adds the marks: OSC 133 A where the prompt
# starts and B where it ends, C when a command starts, D with its exit status when it ends, OSC 633 E
# with the command line and OSC 7 with the directory.
#
# Every mark carries k=<nonce>, a random value per launch; the daemon ignores a mark without it, so
# replaying a recorded session, or a nested shell over ssh, cannot report "finished, exit 0" (fish
# itself may print marks of its own without one, which the daemon ignores the same way). The nonce
# is taken out of the environment here, so programs started from now on do not inherit it.

if not set -q __dsi_loaded
    set -g __dsi_loaded 1
    set -g __dsi_nonce "$DAEDALUS_SI_NONCE"
    set -e DAEDALUS_SI_NONCE

    if test -n "$__dsi_nonce"
        set -g __dsi_ran

        # __dsi_escape prints $argv[1] as an OSC 633 value: a backslash doubled, and ';' and the
        # control characters a command line can hold as \xNN, so it can neither end the sequence
        # nor split its fields. It is cut at 4096 characters first; the daemon keeps no more.
        function __dsi_escape
            set -l s (string sub --length 4096 -- $argv[1] | string collect)
            set s (string replace --all '\\' '\\\\' -- $s | string collect)
            set s (string replace --all \n '\\x0a' -- $s)
            set s (string replace --all ';' '\\x3b' -- $s)
            set s (string replace --all \r '\\x0d' -- $s)
            set s (string replace --all \t '\\x09' -- $s)
            set s (string replace --all \e '\\x1b' -- $s)
            set s (string replace --all \a '\\x07' -- $s)
            printf '%s' $s
        end

        function __dsi_preexec --on-event fish_preexec
            set -g __dsi_ran 1
            set -l line (__dsi_escape "$argv[1]")
            printf '\e]633;E;%s;k=%s\a\e]133;C;k=%s\a' "$line" $__dsi_nonce $__dsi_nonce
        end

        # $status here is the command's; an empty line runs no command and gets no D.
        function __dsi_postexec --on-event fish_postexec
            set -l last $status
            if set -q __dsi_ran[1]
                printf '\e]133;D;%s;k=%s\a' $last $__dsi_nonce
                set -g __dsi_ran
            end
        end

        # Before every prompt: the directory, and A where the prompt is drawn.
        function __dsi_prompt_start --on-event fish_prompt
            printf '\e]7;file://%s%s\a' (prompt_hostname) (string escape --style=url -- $PWD)
            printf '\e]133;A;k=%s\a' $__dsi_nonce
        end

        # B goes at the end of the prompt: the user's fish_prompt is kept under another name and
        # called first thing, so it sees the status it would have seen.
        if functions -q fish_prompt
            functions --copy fish_prompt __dsi_user_prompt
            function fish_prompt
                __dsi_user_prompt
                printf '\e]133;B;k=%s\a' $__dsi_nonce
            end
        end
    end
end
