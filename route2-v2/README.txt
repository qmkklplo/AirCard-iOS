Route 2 V2 - iOS 27 Mercury Configuration Extractor
=====================================================

Purpose
-------
V1 proved that iOS 27 MobileBackup2 exposes Mercury configuration payloads even
though it does not expose a standalone .../descriptors/<UUID>/ tree.

V2 therefore extracts the REAL FILE CONTENTS from the two already-confirmed
Mercury configuration candidates:

  0AEA1C8E-3EA7-4E6E-81E4-511972087A54
  769BEBC4-9C5E-4A57-84C6-939C9C90C521

It intentionally excludes:
  C90158DC-CC4B-4B6B-B86B-997F87CEF0DA

because C901 was previously modified during experiments and should not be used
as the iOS 27 native template candidate.

Safety
------
This tool is READ-ONLY with respect to the iPhone:
- connects through usbmux/lockdown
- requests a MobileBackup2 backup stream
- reads Manifest.db locally
- copies matching payloads on the PC
- creates a ZIP on the PC

It does NOT call restore, rebuild, sparse restore, or PosterBoard write-back.

How to run
----------
1. Extract this package to a normal folder.
2. Connect the iPhone by USB.
3. Unlock it and keep the screen awake.
4. Close GoldenNugget / iTunes / Apple Devices if they are holding the phone.
5. Double-click RUN-Route2-V2.bat.
6. Enter the iPhone passcode if the phone asks during MobileBackup2.
7. Wait for the run to finish.

Output
------
The result is created under:

  Route2-V2-Captures\Route2-V2-YYYYMMDD-HHMMSS\

The important ZIP is named similar to:

  iOS27-Route2-Mercury-Configuration-V2-YYYYMMDD-HHMMSS.zip

Upload that ZIP to ChatGPT.

What V2 preserves
-----------------
For each selected Mercury configuration, V2 copies the complete configuration
payload that MobileBackup2 actually exposed.

It also creates a byte-for-byte "Core7Candidates" view containing, when
available:

  com.apple.posterkit.provider.descriptor.identifier
  com.apple.posterkit.provider.identifierURL.suggestionMetadata.plist
  com.apple.posterkit.role.identifier
  providerInfo.plist
  versions/<n>/contents/.com.apple.posterkit.provider.contents.configurableOptions.plist
  versions/<n>/contents/com.apple.posterkit.provider.contents.otherMetadata.plist
  versions/<n>/contents/com.apple.posterkit.provider.contents.userInfo

No plist is rewritten or normalized. SHA-256 hashes are recorded so the files
can be compared without ambiguity.

Dependency
----------
Pinned to the same pymobiledevice3 version used by the current GoldenNugget
source path tested for this workflow:

  pymobiledevice3==10.7.1
