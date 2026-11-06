Output assets/demo.gif

Set Shell "bash"
Set FontSize 18
Set Width 1100
Set Height 520
Set Padding 24
Set CursorBlink false
Set TypingSpeed 55ms

Type "absentia index fixtures/seed_archive"
Enter
Sleep 2s

Type "absentia check 'Flock 摄像头采购' --archive fixtures/seed_archive"
Enter
Sleep 4s

Type "absentia verify .absentia/attestation.json"
Enter
Sleep 3s
