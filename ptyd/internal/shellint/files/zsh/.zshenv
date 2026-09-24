# Shell integration for zsh. The terminal daemon starts zsh with ZDOTDIR set to this directory,
# because that is the only way to extend an interactive zsh without touching the user's own files.
# Each file here reads the user's file of the same name from where zsh would have found it (the
# user's ZDOTDIR, or home), with ZDOTDIR set as it would be without the daemon; .zshrc then hands
# ZDOTDIR back for good, so zsh reads the user's own .zlogin and a zsh started later starts plainly.
#
# The user's files are sourced at the top level, never from a function: inside a function, a
# `typeset` of theirs would make a local that disappears when the function returns.

typeset -g __dsi_dir=$ZDOTDIR

# The nonce every mark carries (see .zshrc), taken out of the environment before anything the user's
# files start could inherit it.
typeset -g __dsi_nonce=${DAEDALUS_SI_NONCE-}
unset DAEDALUS_SI_NONCE

# Where the user's files live. The daemon passes the ZDOTDIR the shell was started with, if any.
typeset -g __dsi_user_set=${DAEDALUS_USER_ZDOTDIR+1}
typeset -g __dsi_user_zdotdir=${DAEDALUS_USER_ZDOTDIR-}
typeset -g __dsi_user_type=${DAEDALUS_USER_ZDOTDIR+scalar-export}
unset DAEDALUS_USER_ZDOTDIR

# __dsi_enter sets ZDOTDIR as the user's files expect it; __dsi_leave notes what they left it as (a
# .zshenv commonly moves it to ~/.config/zsh) and points zsh back at this directory.
__dsi_enter() {
  if [[ -n $__dsi_user_set ]]; then
    ZDOTDIR=$__dsi_user_zdotdir
  else
    unset ZDOTDIR
  fi
}
__dsi_leave() {
  __dsi_user_set=${ZDOTDIR+1}
  __dsi_user_zdotdir=${ZDOTDIR-}
  __dsi_user_type=${(t)ZDOTDIR}
  ZDOTDIR=$__dsi_dir
}

__dsi_enter
if [[ -f ${ZDOTDIR:-$HOME}/.zshenv ]]; then
  source ${ZDOTDIR:-$HOME}/.zshenv
fi
__dsi_leave
