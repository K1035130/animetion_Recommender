"""Tag 分类规则表。

设计原则：**分流，不是丢弃**。
682 个 doc_freq>=10 的 tag 里，真正的题材标签只是一部分。其余四类
（制作公司 / 年份季度 / 声优监督 / 作品IP）各有归宿：

- STUDIO / STAFF → 不进 tag 向量，但保留下来作为第 4 周 AniList
  structured studios/staff 字段的交叉验证材料
- YEAR / FORM / REGION → 直接丢弃。这些信息已经在 air_year / form /
  meta_tags 列里有结构化版本，重复进 tag 向量只会稀释题材信号
- IP → 丢弃。作品名不是口味特征，且会让同系列作品的相似度虚高
- META → 丢弃。使用行为和情绪评价，不是内容特征

⚠️ 用显式清单而不是纯正则：正则容易误伤。
   「治愈」「催泪」「致郁」看着像元评价，实际是题材（《夏目友人帐》那一类），必须保留。
"""

import re

# ---------------------------------------------------------------- 丢弃类

# 年份 / 季度 / 年代
RE_YEAR = re.compile(
    r"^(?:"
    r"\d{4}"                    # 2016
    r"|\d{4}年"                 # 2017年
    r"|\d{4}年\d{1,2}月"        # 2023年10月
    r"|\d{4}-\d{4}"             # 2010-2019
    r"|\d{4}s"                  # 2010s
    r"|\d{4}年\d{1,2}月新番"
    r"|\d{4}里番"               # 2019里番
    r")$"
)

# ---- 启发式：替代无止境的手工枚举 ----
# 观察：df>=5 的 88 个纯 ASCII tag 里，只有 9 个是题材缩写，其余全是公司/IP。
# 纯假名同理——这个频段的假名 tag 基本都是日本公司名。
RE_ASCII = re.compile(r"^[\x20-\x7E]+$")
# 平假名 + 片假名 + 半角片假名 + 长音/中点/波浪号
RE_KANA = re.compile(r"^[぀-ヿ･-ﾟ～ー・]+$")

# ASCII 白名单：确实是题材/属性的圈内缩写
ASCII_GENRE = {
    "NTR", "R18", "BL", "JK", "SF", "nur", "nür", "chippai", "LOLI", "SM",
    "TS", "GL", "NL",
}

# 形态：air 形态已有 form 列 + meta_tags，tag 版本纯属重复
FORM = {
    "TV", "TVA", "TVSP", "OVA", "OAD", "WEB", "SP", "剧场版", "电影", "动画电影",
    "アニメ映画", "短片", "短篇", "短片集", "动画短片", "小剧场", "动画", "番剧",
    "总集篇", "番外", "外传", "特典", "重制", "续作", "续集", "续期", "第二季",
    "MV", "CM", "广告", "PV", "独立ova", "定格动画", "CG", "3D", "三渲二",
    "日本动画", "美国动画", "中国动画", "国产动画", "欧美动画", "独立动画",
    # df 8–9 段补充
    "年番", "映画", "劇場版アニメ", "动态漫画", "特别篇", "番外篇", "第三季",
    "原创动画", "长剧集", "TVA-02", "歌番", "联动",
}

# 地区：已有 meta_tags 结构化版本
REGION = {
    "日本", "中国", "美国", "欧美", "韩国", "法国", "国产", "国漫", "国创",
    "日漫", "韩漫", "美漫", "中日合作", "三次元",
    # df 8–9 段补充
    "韩国动画", "日美合作", "中日", "英国", "国人", "中国元素",
    "中国原作改编", "韩国原作改编",
}

# 制作公司 / 发行方 / 厂牌（含里番厂牌）
STUDIO = {
    # 日本主要动画公司
    "SUNRISE", "J.C.STAFF", "A-1Pictures", "Production.I.G", "ProductionI.G",
    "BONES", "骨头社", "MADHouse", "MAPPA", "ufotable", "SHAFT", "P.A.WORKS",
    "CloverWorks", "WITSTUDIO", "TRIGGER", "GAINAX", "Khara", "ScienceSARU",
    "动画工房", "動画工房", "京阿尼", "京都动画", "东映", "東映アニメーション",
    "东映动画", "TMS", "TMSEntertainment", "TMS_Entertainment", "OLM",
    "SILVERLINK.", "LIDENFILMS", "Lerche", "Satelight", "XEBEC", "AIC",
    "龙之子", "タツノコプロ", "BrainsBase", "StudioPierrot", "PIERROT",
    "BNPictures", "Feel.", "projectNo.9", "Studio五组", "Studio五組",
    "KINEMACITRUS", "GONZO", "ZEXCS", "8bit", "8-Bit", "davidproduction",
    "ZERO-G", "Diomedéa", "Diomedea", "GoHands", "Passione", "Seven",
    "SevenArcs", "SANZIGEN", "Actas", "DLE", "彗星社", "StudioKAI", "SynergySP",
    "TROYCA", "STUDIOPUYUKAI", "Studio4℃", "GATHERING", "HoodsEntertainment",
    "SIGNAL.MD", "Signal-MD", "Graphinica", "手塚PRODUCTION", "Lay-duce",
    "orange", "Studio3Hz", "辰美", "Engi", "NOMAD", "Manglobe", "Whitebear",
    "C2C", "NAZ", "PashminaA", "millepensee", "旭production", "朱夏",
    "CygamesPictures", "GeekToys", "Arms", "CollaborationWorks", "PINEJAM",
    "FelixFilm", "CONNECT", "MAHOFILM", "asread", "TNK", "亚细亚堂", "BRIDGE",
    "StudioComet", "C-Station", "StudioA-CAT", "OkurutoNoboru",
    "BiburyAnimationStudios", "NUT", "TYOAnimations", "Ordet", "ProductionIMS",
    "Colorido", "StudioColorido", "Genostudio", "SHIN-EI", "シンエイ動画",
    "TelecomAnimationFilm", "PolygonPictures", "ILCA", "神风动画",
    "横滨AnimationLABO", "YostarPictures",
    # 中国
    "玄机科技", "绘梦", "原创动力", "方特动漫", "中影年年", "视美", "若森",
    "有妖气", "腾讯", "大火鸟文化", "艺画开天", "分子互动", "上海美术电影制片厂",
    # 欧美
    "迪士尼", "Disney", "pixar", "皮克斯", "梦工厂", "Marvel", "漫威", "dc",
    "CARTOONNETWORK", "Hasbro", "Netflix", "DCAMU",
    # 出版 / 原作方 / 企划
    "JUMP", "芳文社", "key", "cygames", "Type-Moon", "noitaminA",
    "青年动画制作者育成计划", "动画未来",
    # 里番厂牌
    "ピンクパイナップル", "pinkpineapple", "粉菠萝", "PoRO", "QueenBee",
    "メリー・ジェーン", "鈴木みら乃", "僧侣档", "BOOTLEG", "雷火剣", "雷火剑",
    # df 8–9 段补充
    "studiodeen", "スタジオディーン", "WHITEFOX", "追光动画", "幻维数码",
    "台风Graphics", "颱風グラフィックス", "好传动画", "绘梦动画", "玄机",
    "白组", "CLAMP", "honeyworks", "EDGE", "DMM.futureworks", "EMT2",
    "EMTスクエアード", "EMTSquared", "LapinTrack", "studioMOTHER", "ArtLand",
    "milky", "ZIZ", "Nexus", "AniMan", "W-toonStudio", "studioVOLN",
    "StudioBind", "TOHOanimationSTUDIO", "ばにぃうぉ～か～", "ショーテン",
    "バニラ", "ティーレックス", "七创社", "蓝弧", "娃娃鱼动画", "震雷动画",
    "米哈游", "青青树", "奥飞", "腾讯动漫", "蓝天工作室", "bilibili",
    "若鸿文化", "小疯映画", "天工艺彩", "更三动画", "福煦影视", "万维猫动画",
    "神漫文化", "原力动画", "七灵石", "好传", "视美动画", "广州原创动力文化",
    "日本アニメーション", "京都アニメーション", "サンライズ", "サンジゲン",
    "スタジオコロリド", "スタジオ雲雀", "苇PRODUCTION", "亜細亜堂", "A・C・G・T",
    "龙之子Production", "ファンワークス", "FanWorks", "サエッタ", "彩色铅笔",
    "CoMixWave", "CoMixWaveFilms", "LEVEL5", "StudioMIR", "MaryJane",
    "GOLDBEAR", "EncourageFilms", "CreatorsinPack", "AXsiZ", "domerica",
    "PlatinumVision", "StudioGaina", "drive", "Quad", "Nitro+", "BLADE",
    "IMAGICA", "PieInTheSky", "SUNRISEbeyond", "STUDIOJEMI", "LINDENFILMS",
    "Warner.Bros.Animation", "DreamWorks", "NHK", "Production.IMS", "CRAFTAR",
    "lucasfilm", "GEMBA", "PRA", "Bibury", "Production+h.", "Lesprit",
    "Sublimation", "Eve", "BAKKENRECORD", "StudioKAFKA", "StapleEntertainment",
    "EastFishStudio", "studioHōKIBOSHI", "SEVEN・ARCS", "神風動画", "一迅社",
    "ちちのや", "じゅうしぃまんご～", "Animation", "ppt",
}

# 声优 / 监督 / 脚本 / 音乐 —— 走 AniList staff 结构化字段
STAFF = {
    # 声优
    "花泽香菜", "花澤香菜", "神谷浩史", "杉田智和", "中村悠一", "钉宫理惠",
    "石田彰", "宫野真守", "悠木碧", "福山润", "福山潤", "樱井孝宏", "櫻井孝宏",
    "木村良平", "早見沙織", "早见沙織", "早见沙织", "能登麻美子", "堀江由衣",
    "下野紘", "梶裕貴", "松岡禎丞", "喜多村英梨", "小野大辅", "沢城みゆき",
    "津田健次郎", "梅原裕一郎", "阿澄佳奈", "金元寿子", "入野自由", "小西克幸",
    "水树奈奈", "東山奈央", "佐倉綾音", "石川界人", "雨宮天", "逢坂良太",
    "水岛努",
    # 监督 / 脚本 / 音乐 / 原作者
    "新房昭之", "虚渊玄", "大河内一楼", "岸诚二", "大沼心", "谷口悟朗",
    "神山健治", "汤浅政明", "岡田麿里", "冈田麿里", "吉田玲子", "花田十辉",
    "冲方丁", "西尾维新", "石原立也", "山田尚子", "元永庆太郎", "佐藤顺一",
    "今千秋", "川口敬一郎", "荒木哲郎", "富野由悠季", "庵野秀明", "河森正治",
    "士郎正宗", "荒川弘", "静野孔文", "今石洋之", "高松信司", "山本宽",
    "多田俊介", "黑田洋介", "大地丙太郎", "川井宪次", "泽野弘之", "横山克",
    "成田良悟", "福井晴敏", "西川贵史", "荒木英樹", "市川量也", "横手美智子",
    "水岛精二", "たつき",
    # df 8–9 段补充（2026-08-10 人工审查）
    "佐伯昭志", "永井豪", "佐藤卓哉", "渡边信一郎", "柿原徹也", "柿原彻也",
    "菱田正和", "梶裕贵", "鸟山明", "东山奈央", "小野賢章", "前野智昭",
    "木村隆一", "内田雄馬", "内田真礼", "户松遥", "新海诚", "新海誠",
    "井上麻里奈", "代永翼", "中村春菊", "宮野真守", "子安武人", "梶浦由记",
    "梶浦由記", "菅野洋子", "泽城美雪", "浅香守生", "田村ゆかり", "长井龙雪",
    "佐藤大", "诹访部顺一", "草尾毅", "出渕裕", "出渊裕", "冨岡淳広",
    "岸本卓", "夏目真悟", "山本裕介", "岸誠二", "西村纯二", "小野友樹",
    "小野大輔", "羽原信義", "羽原信义", "花田十輝", "loundraw", "浪川大辅",
    "久米田康治", "奈須きのこ", "石田祐康", "平井久司", "立花慎之介",
    "神前晓", "黒田洋介", "关智一", "荒牧伸志", "丸户史明", "博史池畠",
    "松冈祯丞", "斉藤壮馬", "武田弘光", "小松未可子", "李豪凌", "藤田陽一",
    "榎木淳弥", "高桥李依", "高橋李依", "吉田健一", "太多秀太", "黄伟明",
    "种田梨沙", "森久保祥太郎", "寺本幸代", "石立太一", "板垣伸", "森脇真琴",
    "澤野弘之", "西尾維新", "古橋一浩", "吉野弘幸", "樱井弘明", "桜井弘明",
    "小原好美", "若林信", "青木英", "綾奈ゆにこ", "田中仁", "伊藤史夫",
    "藤本树", "手冢治虫", "佐藤龙雄", "坂本真绫", "平川大輔", "平野绫",
    "高村和宏", "几原邦彦", "细田守", "吉浦康裕", "林原惠", "林原めぐみ",
    "中村健治", "绿川光", "西川貴史", "冈田磨里", "水島精二", "岡本信彦",
    "深崎暮人", "荒木飞吕彦", "千明孝一", "成田良美", "绵田慎也", "弐瓶勉",
    "花江夏树", "花江夏樹", "雨宫天", "石川由依", "鬼頭明里", "牛尾憲輔",
    "牛尾宪辅", "朴性厚", "松尾衡", "田中芳树", "村山功", "小池健",
    "山元隼一", "池添隆博", "高桥留美子", "佐仓绫音", "中原麻衣", "桂正和",
    "贞本义行", "高桥良辅", "汤山邦彦", "吉原正行", "铃木达央", "鈴村健一",
    "饭田马之介", "押井守", "森川智之", "雨宮哲", "渡边步", "麻枝准",
    "晃晃监督", "牧原亮太郎", "茅野爱衣", "荒木英树", "虚淵玄", "釘宮理恵",
    "钉宫理恵", "铃木乃",
}

# 作品名 / IP —— 会让同系列相似度虚高，不是口味特征
IP = {
    "Fate", "高达", "GUNDAM", "pokemon", "宝可梦", "精灵宝可梦", "神奇宝贝",
    "口袋妖怪", "宠物小精灵", "名侦探柯南", "柯南", "名探偵コナン", "海贼王",
    "进击的巨人", "银魂", "蜡笔小新", "哆啦A梦", "鲁邦三世", "宇宙战舰大和号",
    "数码宝贝", "妖精的尾巴", "JOJO", "刀剑神域", "OVERLORD", "物语系列",
    "东方", "东方project", "東方Project", "東方M-1", "喜羊羊与灰太狼", "熊出没",
    "秦时明月", "狐妖小红娘", "斗破苍穹", "鬼灭之刃", "我的英雄学院", "七大罪",
    "排球少年", "龙珠", "攻壳机动队", "强袭魔女", "光之美少女", "プリキュア",
    "LoveLive", "LoveLive！", "偶像大师", "偶像活动", "BanGDream", "赛马娘",
    "彩虹小马", "MLP", "RWBY", "南方公园", "恶搞之家", "蝙蝠侠", "星球大战",
    "ben10", "阴阳师", "吉卜力", "暗芝居", "鬼父", "乐高", "k", "超英",
    # df 8–9 段补充
    "摇曳百合", "网球王子", "侦探歌剧", "英雄联盟", "约会大作战", "少女与战车",
    "美妙系列", "美妙旋律", "美妙天堂", "斗罗大陆", "黄金神威", "画江湖",
    "璀璨星空", "关于我转生变成史莱姆这档事", "卡片战斗先导者", "游戏王",
    "遊☆戯☆王", "魔法少女小圆", "猫和老鼠", "JOJO的奇妙冒险", "结城友奈是勇者",
    "期待在地下城邂逅有错吗", "赛尔号", "亚人", "文豪野犬", "刀剑乱舞",
    "一人之下", "非人哉", "瑞克和莫蒂", "马男波杰克", "月虹", "海绵宝宝",
    "火影忍者", "夏目友人帐", "黑子的篮球", "美少女战士", "头文字D", "暗杀教室",
    "食戟之灵", "奥特曼", "刃牙", "哥斯拉", "猪猪侠", "开心超人", "我叫MT",
    "妄想学生会", "魔法少女☆伊莉雅", "舰娘", "女王蜂", "阿松", "全职高手",
    "少年歌行", "凡人修仙传", "明日方舟", "崩坏3", "刺客伍六七", "炎炎消防队",
    "灵能百分百", "京剧猫", "茶啊二中", "幼女战记", "魔法使的新娘", "黑执事",
    "东京喰种", "一拳超人", "钻石王牌", "鬼灯的冷彻", "牙狼", "弹丸论破",
    "加速世界", "女皇之刃", "空之境界", "只有神知道的世界", "传说系列",
    "宝石宠物", "功夫熊猫", "小黄人", "芭比", "蜘蛛侠", "史蒂文的宇宙",
    "宇宙小子", "银河英雄传说", "銀河英雄伝説", "宇宙戦艦ヤマト2199",
    "魔法科高校的劣等生", "我的青春恋爱物语果然有问题", "元气少女缘结神",
    "永远之久远", "来自深渊", "恶魔城", "生化危机", "女神异闻录", "超时空要塞",
    "剑风传奇", "青之驱魔师", "中二病也要谈恋爱", "鲁鲁修", "反叛的鲁路修",
    "闪电十一人", "闪电十一人GO", "阿卡林", "喵帕斯", "悠哉日常大王",
    "黑白小姐", "兽娘动物园", "灵域", "那年那兔那些事儿", "中国唱诗班",
    "噬血狂袭", "魁拔", "新选组", "邦邦", "Q娃", "王牌", "IM@S",
    "digimon", "precure", "ONEPIECE", "LOL", "FGO", "PSYCHO-PASS",
    "Free!", "My_Little_Pony", "SouthPark", "DC.Comics", "AdventureTime",
    "WIXOSS", "Macross", "IDOLiSH7", "MMD", "vtuber",
}

# 元评价 / 使用行为 / 情绪吐槽 —— 不是内容特征
# ⚠️ 「治愈」「催泪」「致郁」不在此列，它们是题材
META = {
    "实用", "超高实用性", "有生之年", "垃圾", "下限", "厕纸", "补标", "糟糕",
    "节操", "装逼", "沙雕", "脑洞", "燃", "党争", "辱华", "讽刺", "萌豚",
    "开大车", "补番", "神作", "经典", "好评", "力荐", "雷", "坑",
    # df 8–9 段补充
    "声优", "奥斯卡", "无", "废萌", "惨遭动画化", "虐狗", "逆天", "作画崩坏",
    "抄袭", "答辩", "好人设", "爽文", "无下限", "童年补完计划",
    "无法预测的命运之舞台", "里番也放上来大丈夫？", "FANS向", "未确定", "其它",
    "退队流", "伪",
}

DISCARD_SETS = {
    "FORM": FORM,
    "REGION": REGION,
    "IP": IP,
    "META": META,
}
# 这两类不进 tag 向量，但留作 AniList 交叉验证
DIVERT_SETS = {
    "STUDIO": STUDIO,
    "STAFF": STAFF,
}

# ---------------------------------------------------------------- 同义合并

# 归并到 → 标准写法。只合并**确定同义**的，存疑的一律不动。
SYNONYM = {
    # 改编来源（最大的一对：漫画改+漫改 覆盖 59% 候选集）
    "漫改": "漫画改",
    "轻改": "轻小说改",
    "游戏改编": "游戏改",
    "游改": "游戏改",
    "GAL改": "游戏改",
    "手游改": "游戏改",
    # 题材
    "治愈系": "治愈",
    "泡面": "泡面番",
    "萌系": "萌",
    "体育": "运动",
    "少女系": "少女向",
    "腐": "腐向",
    # R18（保留，但统一写法）
    "無修正": "无码",
    "肉番": "卖肉",
    "肉": "卖肉",
    "18X": "18禁",
    "H": "18禁",
    "里": "里番",
    "2D里番": "里番",
    "重口味": "重口",
}

# ⚠️ 明确**不合并**的易混对，写下来防止以后手贱：
#   小说改 ≠ 轻小说改   （一般小说 vs 轻小说，受众差别很大）
#   百合   ≠ 轻百合     （后者是「疑似百合」的暧昧向，强度不同）
#   后宫   ≠ 逆后宫     （性别对象相反）
#   萝莉   ≠ 幼女       （社区用法有区别，且幼女向是子供向的意思）


def classify(tag: str) -> str:
    """返回 tag 的类别：KEEP / YEAR / FORM / REGION / STUDIO / STAFF / IP / META

    显式清单优先，启发式兜底。顺序不能反——ASCII_GENRE 白名单必须
    先于 RE_ASCII 生效，否则 NTR/BL/JK 会被误判成公司。
    """
    if RE_YEAR.match(tag):
        return "YEAR"
    for label, s in DIVERT_SETS.items():
        if tag in s:
            return label
    for label, s in DISCARD_SETS.items():
        if tag in s:
            return label

    # 启发式兜底：避免随阈值下降无止境地手工枚举公司名
    if tag in ASCII_GENRE:
        return "KEEP"
    if RE_ASCII.match(tag) or RE_KANA.match(tag):
        return "STUDIO"
    return "KEEP"


def normalize(tag: str) -> str:
    """同义合并。"""
    return SYNONYM.get(tag, tag)


# ---------------------------------------------------------------- 自检
# 规则表是手工维护的，补条目时很容易和已有的撞。这里在导入时就把
# **有害**的那类冲突拦下来。
#
# 注意：集合内的字面量重复（同一个 set 里写了两遍）无法在运行时检出——
# Python 的 set 字面量已经去重了。要查那类问题跑：
#   uv run python -c "import ast,collections;
#     [print(n.targets[0].id, [k for k,v in collections.Counter(
#       [e.value for e in n.value.elts]).items() if v>1])
#      for n in ast.walk(ast.parse(open('src/tag_rules.py',encoding='utf-8').read()))
#      if isinstance(n, ast.Assign) and isinstance(n.value, ast.Set)]"


def _selfcheck() -> None:
    all_sets = {
        "STUDIO": STUDIO, "STAFF": STAFF, "IP": IP,
        "FORM": FORM, "REGION": REGION, "META": META,
        "ASCII_GENRE": ASCII_GENRE,
    }
    # ① 跨集合重复：classify() 会按 DIVERT→DISCARD 顺序静默裁决，
    #    结果取决于字典顺序而非本意 —— 必须唯一归属
    owner: dict[str, list[str]] = {}
    for label, s in all_sets.items():
        for t in s:
            owner.setdefault(t, []).append(label)
    cross = {t: ls for t, ls in owner.items() if len(ls) > 1}
    if cross:
        raise AssertionError(f"tag 同时归属多个集合，归属必须唯一: {cross}")

    # ② 同义词自指：无效条目，通常是复制粘贴的残留
    selfref = {k for k, v in SYNONYM.items() if k == v}
    if selfref:
        raise AssertionError(f"SYNONYM 自指条目: {selfref}")

    # ③ 同义词链：normalize() 只走一步，A→B→C 会停在 B，静默出错
    chained = {k: v for k, v in SYNONYM.items() if v in SYNONYM}
    if chained:
        raise AssertionError(f"SYNONYM 存在链式归并（normalize 只走一步）: {chained}")

    # ④ 归并目标本身被判为丢弃类 —— 说明合并方向反了
    bad = {k: v for k, v in SYNONYM.items() if classify(v) != "KEEP"}
    if bad:
        raise AssertionError(f"SYNONYM 归并到了非 KEEP 的标准名: {bad}")


_selfcheck()
