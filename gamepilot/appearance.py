"""Dark sidebar and cards for the existing controls; no automation features."""
import tkinter as tk
from tkinter import ttk
from version import VERSION

BG, CARD, TEXT, MUTED, ACCENT = '#202124', '#2c2e33', '#f1f3f7', '#a5adbb', '#d6bb7a'


def build(app):
    root = app.root
    root.configure(background=BG)
    root.minsize(1000, 700)
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('.', font=('Microsoft YaHei UI', 10), background=BG, foreground=TEXT)
    style.configure('TFrame', background=BG)
    style.configure('Card.TFrame', background=CARD)
    style.configure('TLabel', background=CARD, foreground=TEXT)
    style.configure('Muted.TLabel', foreground=MUTED)
    style.configure('TButton', background='#3a3e46', foreground=TEXT, borderwidth=0, padding=(14, 9))
    style.map('TButton', background=[('active', '#4a5260'), ('disabled', '#303238')], foreground=[('disabled', '#727780')])
    style.configure('Accent.TButton', background=ACCENT, foreground='#142737', font=('Microsoft YaHei UI', 10, 'bold'))
    style.map('Accent.TButton', background=[('active', '#96d8ff'), ('disabled', '#3b5463')])
    style.configure('TCheckbutton', background=CARD, foreground=TEXT, padding=5)
    style.map('TCheckbutton', background=[('active', CARD)], foreground=[('disabled', MUTED)])
    style.configure('TEntry', fieldbackground='#1f2227', foreground=TEXT, insertcolor=TEXT, padding=6)
    style.configure('TCombobox', fieldbackground='#1f2227', foreground=TEXT, padding=6)
    style.map('TCombobox', fieldbackground=[('readonly', '#1f2227')], foreground=[('readonly', TEXT)])

    sidebar = tk.Frame(root, bg='#191a1d', width=174)
    sidebar.pack(side='left', fill='y'); sidebar.pack_propagate(False)
    tk.Label(sidebar, text='◈  GamePilot', font=('Segoe UI', 14, 'bold'), fg=TEXT, bg='#191a1d').pack(anchor='w', padx=16, pady=(28, 3))
    tk.Label(sidebar, text=VERSION, fg=ACCENT, bg='#191a1d', font=('Segoe UI', 9)).pack(anchor='w', padx=21, pady=(0, 32))
    main = ttk.Frame(root, padding=(20, 18))
    main.pack(side='left', fill='both', expand=True)
    pages = [ttk.Frame(main) for _ in range(3)]
    nav = []
    def show(index):
        for i, page in enumerate(pages):
            page.pack_forget()
            nav[i].configure(bg='#30333a' if i == index else '#191a1d', fg=ACCENT if i == index else TEXT)
        pages[index].pack(fill='both', expand=True)
    for index, title in enumerate(('▷   运行控制', '⚙   识别设置', '≡   运行记录')):
        button = tk.Button(sidebar, text=title, command=lambda i=index: show(i), anchor='w', relief='flat', bd=0,
                           bg='#191a1d', fg=TEXT, activebackground='#30333a', activeforeground=ACCENT,
                           font=('Microsoft YaHei UI', 11), padx=19, pady=13, cursor='hand2')
        button.pack(fill='x', padx=8, pady=3); nav.append(button)
    tk.Label(sidebar, text='F8  紧急停止\n\n点“开始自动执行”运行\n或选择“仅识别预览”', justify='left', wraplength=146,
             fg=MUTED, bg='#191a1d', font=('Microsoft YaHei UI', 9)).pack(side='bottom', anchor='w', padx=18, pady=24)
    home, settings, logs = pages
    banner = tk.Canvas(home, height=132, bg='#202d30', highlightthickness=0)
    banner.pack(fill='x', pady=(0, 14))
    # Code-native resonance rings, wave trace and distant silhouettes.
    banner.create_polygon(400,132,480,70,520,97,580,38,630,94,690,50,760,103,900,45,900,132,fill='#2c4245',outline='')
    banner.create_polygon(470,132,550,90,610,113,705,76,760,110,900,78,900,132,fill='#1b282c',outline='')
    for radius in (36,57,79):
        banner.create_arc(696-radius,65-radius,696+radius,65+radius,start=22,extent=292,outline='#8a8666',width=1,style='arc')
    banner.create_oval(689,58,703,72,fill=ACCENT,outline='')
    banner.create_line(540,65,610,65,632,54,650,80,675,30,696,96,715,54,736,67,840,67,fill=ACCENT,width=2,smooth=False)
    banner.create_text(240,38,text=VERSION,anchor='w',fill=ACCENT,font=('Segoe UI',11))
    banner.create_text(24, 35, text='GamePilot', anchor='w', fill=TEXT, font=('Segoe UI', 26, 'bold'))
    banner.create_text(26, 77, text='鸣潮 · 周常乐园', anchor='w', fill='#e4d5af', font=('Microsoft YaHei UI', 13))
    banner.create_text(26,107,text='循声而行  /  识别当前页面，执行下一步',anchor='w',fill='#a6baba',font=('Microsoft YaHei UI',9))
    controls = ttk.Frame(home, style='Card.TFrame', padding=12); controls.pack(fill='x', pady=(0, 10))
    app.buttons = []
    for title, callback in (('▷  开始自动执行', app.start_automation), ('暂停', app.pause), ('继续', app.resume), ('停止', app.stop)):
        button = ttk.Button(controls, text=title, command=callback, style='Accent.TButton' if callback == app.start_automation else 'TButton')
        button.pack(side='left', padx=(0, 8)); app.buttons.append(button)
    app.mode_hint = tk.StringVar(value='仅识别 · 不发送输入')
    ttk.Label(controls, textvariable=app.mode_hint, wraplength=400, style='Muted.TLabel').pack(side='left', padx=8)
    card = ttk.Frame(home, style='Card.TFrame', padding=10); card.pack(fill='x', pady=(0, 10))
    app.real_enabled = tk.BooleanVar(value=False)
    app.real_checkbox = ttk.Checkbutton(card, text='允许本次实际操作（自动循环 / 单步）', variable=app.real_enabled, command=app.refresh_mode)
    app.real_checkbox.pack(anchor='w')
    row = ttk.Frame(card, style='Card.TFrame'); row.pack(fill='x', pady=(5, 0))
    app.step_target = tk.StringVar(value='P1')
    ttk.Label(row, text='单步目标').pack(side='left', padx=5)
    ttk.Combobox(row, textvariable=app.step_target, values=('P1','P2','P3','P4','P5'), state='readonly', width=5).pack(side='left', padx=8)
    app.step_button = ttk.Button(row, text='单步测试', command=app.single_step); app.step_button.pack(side='left', padx=6)
    app.buttons.append(app.step_button)
    ttk.Button(row, text='仅识别预览', command=app.start_preview).pack(side='left', padx=6)
    ttk.Button(row, text='图片测试', command=app.open_image).pack(side='left', padx=6)
    app.status = tk.StringVar(value='准备就绪。先到识别设置中定位游戏窗口。')
    app.activity = tk.StringVar(value='最近动作：尚未执行')
    for variable in (app.status, app.activity):
        ttk.Label(home, textvariable=variable, padding=10, wraplength=860, style='Muted.TLabel').pack(fill='x', pady=(0, 6))
    app.canvas = ttk.Label(home, text='画面预览\n开始识别或打开参考图片后显示', anchor='center', style='Muted.TLabel')
    app.canvas.pack(fill='both', expand=True)

    title = tk.Label(settings, text='识别设置', bg=BG, fg=TEXT, font=('Microsoft YaHei UI', 22, 'bold'))
    title.pack(anchor='w', pady=(8, 20))
    box = ttk.Frame(settings, style='Card.TFrame', padding=20); box.pack(fill='x')
    ttk.Label(box, text='游戏截图区域', font=('Microsoft YaHei UI', 12, 'bold')).pack(anchor='w')
    ttk.Label(box, text='左、上、宽、高（物理像素）；建议使用自动定位', style='Muted.TLabel').pack(anchor='w', pady=(8, 12))
    app.region = tk.StringVar(value=','.join(map(str, app.config['region'])) if app.config['region'] else '')
    ttk.Entry(box, textvariable=app.region, width=40).pack(anchor='w', fill='x', pady=(0, 14))
    ttk.Button(box, text='自动定位游戏', command=app.locate_game, style='Accent.TButton').pack(anchor='w')
    options = ttk.Frame(settings, style='Card.TFrame', padding=20); options.pack(fill='x', pady=14)
    app.anchor_value = tk.StringVar(value=str(app.config['anchor_threshold']))
    app.button_value = tk.StringVar(value=str(app.config['button_threshold']))
    app.interval_value = tk.StringVar(value=str(app.config['interval_ms']))
    for index, (label, variable) in enumerate((('锚点阈值',app.anchor_value),('按钮阈值',app.button_value),('预览间隔 ms',app.interval_value))):
        ttk.Label(options, text=label).grid(row=index,column=0,sticky='w',pady=8,padx=(0,30))
        ttk.Entry(options, textvariable=variable, width=18).grid(row=index,column=1,sticky='w',pady=8)
    app.p6_enabled = tk.BooleanVar(value=app.config['p6_enabled'])
    ttk.Checkbutton(options,text='启用 P6', variable=app.p6_enabled).grid(row=3,column=0,columnspan=2,sticky='w',pady=10)
    ttk.Button(settings,text='保存配置',command=app.save,style='Accent.TButton').pack(anchor='w')
    tk.Label(logs,text='运行记录',bg=BG,fg=TEXT,font=('Microsoft YaHei UI',22,'bold')).pack(anchor='w',pady=(8,20))
    app.log = tk.Text(logs, state='disabled', wrap='word', bg=CARD, fg='#c5d3e3', relief='flat', padx=16,pady=16,font=('Consolas',10))
    scroll = ttk.Scrollbar(logs, command=app.log.yview); scroll.pack(side='right',fill='y')
    app.log.configure(yscrollcommand=scroll.set); app.log.pack(fill='both',expand=True)
    app.pages, app.show_page = pages, show
    show(0)
