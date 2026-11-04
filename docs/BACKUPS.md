# Workstation backups on the MicroServer

## Processing and placement

The selected design runs Restic on Threadripper. The workstation reads the
reviewed source files, deduplicates content, compresses new data and encrypts it
before transmission. The MicroServer receives and stores the encrypted
repository. Backup compression, encryption, repository checks and pruning run
on the workstation. NAS filesystem integrity checks and snapshots run on the NAS.

Use Restic repository format 2 with `--compression auto` as the starting point.
Measure CPU use, duration and transferred bytes before choosing stronger
compression. Previously compressed files may offer little additional saving.
See [Restic compression settings](https://restic.readthedocs.io/en/stable/047_tuning_parameters.html#compression)
and [repository formats](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html).

The workstation project already owns Restic clients, protected credentials,
include/exclude policies and application-consistent service recovery. Reuse
those controls when adding the NAS destination. Its existing S3 and local-SSD
recovery paths are not an activated NAS job. A NAS destination needs its own
verified mount or endpoint, permissions and recovery test. Preserve the current
local-SSD identity checks and working recovery copies during that integration.

## Decryption secrets and recovery

Keep the repository password and usable decryption keys on the workstation,
through its protected credential workflow. Keep an independent recovery copy
in the owner's password manager or protected offline storage so workstation
failure does not also lose the ability to restore.

Do not place those secrets in a NAS account, share, installer image, inventory,
Git, command-line argument or log. Exclude the workstation credential stores
from the ordinary backup selection. Restic's repository includes password-wrapped
key records; storing those records on the NAS is expected and does not give it
the decryption password. Preserve the complete repository structure.
See [Restic's key design and threat model](https://restic.readthedocs.io/en/stable/100_references.html).

## Shares and storage

This policy applies to Restic backup repositories. A document copied directly
to an ordinary NFS share is not encrypted by this design. An unlocked NAS
dataset can still serve ordinary files normally. SSH/TLS or another reviewed
transport policy is a separate choice; current NFS exports do not encrypt
ordinary share traffic.

Keep LZ4 for ordinary shared data. When preparing a dedicated backup dataset,
use `compression=off` and `dedup=off` because the repository content is already
compressed and encrypted. Do not change the shared dataset globally or migrate
existing data as part of recording this policy. The current role creates only
the shared dataset; backup dataset creation remains a reviewed owner step.

Encryption protects backup confidentiality. A writable repository can still be
deleted by an account with sufficient access. Qualify independent NAS snapshot
retention and a separate recovery copy alongside the backup workflow.

## Activation and qualification

The NAS profile records this design and stages the public guide. It does not
install Restic on the NAS, create a repository, generate a password or enable a
workstation backup job. Complete these checks after the NAS is installed:

1. Select the workstation sources and prepare application-consistent captures
   using the existing recovery workflow. Review credential/cache exclusions.
2. Verify the exact NAS endpoint or mounted export, repository path and numeric
   ownership. A missing or wrong mount must stop the job before repository
   creation or writes; never fall back to a directory on the workstation SSD.
3. Prepare the password and independent recovery copy locally. Initialize only
   a new, owner-selected repository; preserve any existing repository.
4. Run an initial backup and an incremental backup from the workstation. Check
   repository format, compression mode, repository integrity and job results.
5. Restore selected files into a separate scratch directory. Compare contents
   and required metadata, and verify access using the independent recovery copy.
6. Review retention and maintenance schedules before enabling jobs. Measure
   workstation load, NAS load and transferred bytes; record the results privately.

Until those checks pass, NAS backup activation and recovery remain unverified.
