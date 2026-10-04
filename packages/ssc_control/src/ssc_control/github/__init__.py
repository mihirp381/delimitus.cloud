"""The GitHub App (SSC-047): connect a repository once, every push to its branch deploys
preview, and prod still changes only through promote.

* ``client``: the REST calls, with the App's RS256 JWT and one-hour installation tokens minted
  per repository and permission, kept for at most 50 minutes, never stored or logged.
* ``webhook``: the shared-secret signature check and the one event that builds, a push to a
  branch. A pull request, from a fork or not, never builds.
* ``links``: installations bound to an org by an operator, and each app's connected repository.
* ``source``: the commit's tarball unpacked without its top folder and packed as a bundle the
  same way ``ssc deploy`` packs a folder.
* ``push``: the job a push defers. It stores the bundle after every ``complete`` check, the
  secret scan among them, builds and deploys preview, and posts the result and the preview
  address on the commit as the ``SSC / preview`` check run.
* ``gate``: the named checks an app requires before promote, each bound to its workflow file
  and the connected branch.
"""
