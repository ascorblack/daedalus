# Shell integration for zsh: the user's .zshrc, then the marks. OSC 133 A where the prompt starts
# and B where it ends, C when a command starts, D with its exit status when it ends, OSC 633 E with
# the command line and OSC 7 with the directory. Every mark carries k=<nonce>, a random value per
# launch; the daemon ignores a mark without it, so replaying a recorded session, or a nested shell
# over ssh, cannot report "finished, exit 0".
#
# The hooks are added to precmd_functions and preexec_functions, beside the user's, never instead.

__dsi_enter
if [[ -f ${ZDOTDIR:-$HOME}/.zshrc ]]; then
  source ${ZDOTDIR:-$HOME}/.zshrc
fi
__dsi_leave

# ZDOTDIR goes back to what it would be without the daemon, for good: zsh then reads the user's own
# .zlogin, and the history and completion files a framework keeps under ZDOTDIR stay the user's.
if [[ -n $__dsi_user_set ]]; then
  ZDOTDIR=$__dsi_user_zdotdir
  if [[ $__dsi_user_type == *export* ]]; then
    export ZDOTDIR
  fi
else
  unset ZDOTDIR
fi
# A history file named under our directory was named by a system zshrc that ran before this one,
# while ZDOTDIR was still ours: macOS's /etc/zshrc sets HISTFILE=${ZDOTDIR:-$HOME}/.zsh_history. It
# goes where that line would have put it without the daemon, not into the daemon's state directory.
if [[ ${HISTFILE-} == $__dsi_dir/* ]]; then
  HISTFILE=${ZDOTDIR:-$HOME}/${HISTFILE#$__dsi_dir/}
fi
unfunction __dsi_enter __dsi_leave
unset __dsi_dir __dsi_user_set __dsi_user_zdotdir __dsi_user_type

if [[ -n $__dsi_nonce ]]; then
  typeset -g __dsi_ran= __dsi_last=0 __dsi_cwd_seen= __dsi_cwd_url=
  typeset -g __dsi_b=$'\e]133;B;k='$__dsi_nonce$'\a'

  # __dsi_escape sets REPLY to $1 as an OSC 633 value: a backslash doubled, and ';' and every control
  # character as \xNN, so the command line can neither end the sequence nor split its fields. It is
  # cut at 4096 characters first; the daemon keeps no more than that.
  __dsi_escape() {
    emulate -L zsh
    local s=${1[1,4096]} out= c
    local -i i
    s=${s//\\/\\\\}
    s=${s//;/\\x3b}
    if [[ $s == *[[:cntrl:]]* ]]; then
      for (( i = 1; i <= $#s; i++ )); do
        c=$s[i]
        if [[ $c == [[:cntrl:]] ]]; then
          out+="\\x${(l:2::0:)$(( [##16] #c ))}"
        else
          out+=$c
        fi
      done
      s=$out
    fi
    REPLY=$s
  }

  # __dsi_cwd prints OSC 7 for the working directory, percent-encoded byte by byte; the encoding is
  # redone only when the directory changed.
  __dsi_cwd() {
    emulate -L zsh
    if [[ $PWD != "$__dsi_cwd_seen" ]]; then
      setopt local_options no_multibyte
      local s=$PWD out= c
      local -i i
      for (( i = 1; i <= $#s; i++ )); do
        c=$s[i]
        if [[ $c == [a-zA-Z0-9/._~-] ]]; then
          out+=$c
        else
          out+="%${(l:2::0:)$(( [##16] (#c) & 255 ))}"
        fi
      done
      __dsi_cwd_seen=$PWD
      __dsi_cwd_url=$out
    fi
    builtin printf '\e]7;file://%s%s\a' "${HOST-}" "$__dsi_cwd_url"
  }

  # The first precmd hook, so the status is the command's and not another hook's.
  __dsi_status() {
    __dsi_last=$?
  }

  # The last precmd hook: D for the command that ran (none after an empty line), the directory, B at
  # the end of the prompt a theme may have rebuilt by now, and A where the prompt is drawn next.
  __dsi_precmd() {
    if [[ -n $__dsi_ran ]]; then
      builtin printf '\e]133;D;%s;k=%s\a' "$__dsi_last" "$__dsi_nonce"
      __dsi_ran=
    fi
    __dsi_cwd
    if [[ $PS1 != *"$__dsi_b"* ]]; then
      PS1="${PS1}%{${__dsi_b}%}"
    fi
    builtin printf '\e]133;A;k=%s\a' "$__dsi_nonce"
  }

  __dsi_preexec() {
    __dsi_ran=1
    __dsi_escape "$1"
    builtin printf '\e]633;E;%s;k=%s\a\e]133;C;k=%s\a' "$REPLY" "$__dsi_nonce" "$__dsi_nonce"
  }

  precmd_functions=(__dsi_status $precmd_functions __dsi_precmd)
  preexec_functions+=(__dsi_preexec)
fi
