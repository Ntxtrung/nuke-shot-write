# Nuke Shot Write

Action Ring tạo Write node và đặt đường dẫn render theo shot của script Nuke đã lưu.

## Cài đặt

Tải repo về máy. Trong Script Editor của Nuke, chọn Python và chạy lệnh sau với đường dẫn đến file đã tải.

```python
import runpy
runpy.run_path(r"C:\path\to\nuke-shot-write\shot_write.py")["install"]()
```

Script cài `shot_write.py` và thêm lệnh nạp vào `menu.py` trong thư mục `.nuke` của người dùng. `menu.py` hiện có được sao lưu trước khi thêm lệnh.

## Sử dụng

Lưu script `.nk`, chọn node ảnh nguồn và bấm **Shift+Alt+W** trong Node Graph.

- PNG, EXR và DPX cho chọn **1 Frame** hoặc **Sequence**, rồi chọn tên output.
- MOV dùng H.264, chất lượng High, rồi chọn tên output.
- Các tên gồm `toRunAI`, `preRender_00x`, `endComp`, `endPaint` và `toTrack`.
- `preRender_00x` tự lấy số tiếp theo từ các Write và thư mục output của shot.
- Với 1 Frame, dùng nút **Render Frame** trong tab **Shot Write** của node để render frame đã chọn.

Khi `.nk` nằm trong thư mục `nuke`, output nằm trong thư mục `preRender` cùng cấp. Tên shot lấy từ thư mục cha của `nuke`. Với script nằm ở thư mục khác, thư mục chứa script được dùng làm thư mục shot.

Ví dụ sequence EXR và ảnh PNG frame 1001 lần xuất đầu tiên có đường dẫn như sau.

```text
<shot>/preRender/exr/<shot>_endComp/<shot>_endComp.####.exr
<shot>/preRender/png/<shot>_toRunAI_1001_001/<shot>_toRunAI_1001_001.png
```

## Màu và thông số plate

Raw và Limit to range được tắt. PNG và MOV lấy output transform từ Viewer khi ánh xạ được bằng OCIO config. EXR mặc định dùng working space, half-float 16-bit và ZIP 1 scanline. DPX mặc định dùng 10-bit và LUT log của project.

Nếu template có node Read tên `Read_Plate` ở cấp Root và plate cùng định dạng xuất, tool đọc header để lấy datatype và compression EXR, đồng thời ưu tiên colorspace đã khai báo trên Read. EXR multipart lấy thông số của part đầu tiên. Khi thiếu nguồn phù hợp, tool dùng preset mặc định.

Chọn Write rồi dùng **Shot Write → Update Selected Writes** để áp dụng thiết lập màu và After Render cho node hiện có.

## Render xong và tạo Read

After Render chuẩn bị một Read node trên clipboard. Bấm **Ctrl+V** trong Node Graph để dán Read với đường dẫn, Frame Range, Original Range, format và colorspace của output. Range lấy từ các file hiện có trên đĩa, format lấy từ một frame thật.

Callback clipboard chạy trong phiên Nuke GUI. Với luồng này, bỏ chọn **Render using Frame Server** trong hộp thoại render. **Ctrl+Alt+R** cũng tạo Read từ metadata clipboard của Shot Write.

## Kiểm tra

Đã kiểm tra giao diện bằng Qt đi kèm Nuke 15.1 và 17.1. Logic tạo node và clipboard được kiểm tra bằng API Nuke mô phỏng cùng các file ảnh thật. Thao tác render và dán node trong phiên Nuke có license cần xác nhận trên máy sử dụng.
