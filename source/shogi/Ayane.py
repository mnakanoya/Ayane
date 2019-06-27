import threading
import subprocess
import time
import os
from queue import Queue
from enum import Enum

# これらのクラスを用いるには、USIプロトコルについてある程度理解している必要があります。
# 「USIプロトコル」でググってください。


# UsiEngineクラスのなかで用いるエンジンの状態を表現するenum
class UsiEngineState(Enum):
    WaitConnecting  = 1     # 起動待ち
    Connected       = 2     # 起動完了
    WaitReadyOk     = 3     # "readyok"待ち
    WaitCommand     = 4     # "position"コマンド等を送信できる状態になった
    WaitBestmove    = 5     # "go"コマンドを送ったので"bestmove"が返ってくるのを待っている状態
    Disconnected    = 999   # 終了した


# 評価値(Eval)を表現するenum
class UsiEvalValue(Enum):

	# 0手詰めのスコア(rootで詰んでいるときのscore)
	# 例えば、3手詰めならこの値より3少ない。
    ValueMate = 100000

	# Valueの取りうる最大値(最小値はこの符号を反転させた値)
    ValueInfinite = 100001

	# 無効な値
    ValueNone = 100002

    # ply手詰みのスコアを数値化する    
    # ply : integer
    @staticmethod
    def mate_in_ply(ply : int):
        return int(UsiEvalValue.ValueMate) - ply

    # ply手で詰まされるスコアを数値化する
    # ply : integer
    @staticmethod
    def mated_in_ply(ply : int):
        return -int(UsiEvalValue.ValueMate) + ply



# 思考エンジンから送られてきた読み筋を表現するクラス。
# "info pv ..."を解釈したもの。
class UsiThinkPV():

    # 評価値(整数値) : UsiEvalValue型
    eval = None

    # PV文字列。最善応手列。sfen表記文字列にて。
    # 例 : "7g7f 8c8d"みたいなの。あとは、split()して使ってもらえればと。
    # sfen以外の特殊表記として以下の文字列が混じっていることがあります。(やねうら王のdocs/解説.txtを参考にすること。)
    #  "rep_draw" : 普通の千日手
    #  "rep_sup"  : 優等局面(盤上の駒配置が同一で手駒が一方的に増えている局面への突入。相手からの歩の成り捨て～同金～歩打ち～金引きみたいな循環)
    #  "rep_inf"  : 劣等局面(盤上の駒配置が同一で手駒が一方的に減っている局面への突入)
    #  "rep_win"  : 王手を含む千日手(反則勝ち) // これ実際には出力されないはずだが…。
    #  "rep_lose" : 王手を含む千日手(反則負け) // これ実際には出力されないはずだが…。
    #  読み筋が宣言勝ちのときは読み筋の末尾に "win"
    #  投了の局面で呼び出されたとき "resign"
    pv = None


# 思考エンジンに対して送った"go"コマンドに対して思考エンジンから返ってきた情報を保持する構造体
class UsiThinkResult():
    # 最善手(sfen表記文字列にて。例:"7g7f")
    # "bestmove"を受信するまではNoneが代入されている。
    # "resign"(投了) , "win"(宣言勝ち) のような文字列もありうる。
    bestmove = None

    # 最善手を指したあとの相手の指し手。(sfen表記文字列にて)
    # ない場合は、文字列で"none"。
    ponder = None

    # 最善応手列
    # UsiThinkPVの配列。
    # MultiPVのとき、その数だけ返ってくる。
    # 最後に送られてきた読み筋がここに格納される。
    pvs = [] # List[UsiThinkPv]


# USIプロトコルを用いて思考エンジンとやりとりするためのwrapperクラス
class UsiEngine():

    # 通信内容をprintで表示する(デバッグ用)
    debug_print = True

    # エンジン側から"Error"が含まれている文字列が返ってきたら、それをprintで表示する。
    # これはTrueにしておくのがお勧め。
    error_print = True

    # --- readonly members ---

    # エンジンの格納フォルダ
    # Connect()を呼び出したときに代入される。(readonly)
    engine_path = None
    engine_fullpath = None

    # エンジンとのやりとりの状態を表現する。(readonly)
    # UsiEngineState型
    engine_state = None
    
    # connect()のあと、エンジンが終了したときの状態
    # エラーがあったとき、ここにエラーメッセージ文字列が入る
    # エラーがなく終了したのであれば0が入る。(readonly)
    exit_state = None


    # engineに渡すOptionを設定する。
    # 基本的にエンジンは"engine_options.txt"で設定するが、Threads、Hashなどあとから指定したいものもあるので
    # それらについては、connectの前にこのメソッドを呼び出して設定しておく。
    # 例) usi.set_option({"Hash":"128","Threads":"8"})
    def set_options(self,options):
        if type(options) is not dict:
            raise TypeError("options must be dict.")
        self.__options = options


    # エンジンに接続する
    # enginePath : エンジンPathを指定する。
    # エンジンが存在しないときは例外がでる。
    def connect(self, engine_path):
        self.disconnect()

        self.engine_path = engine_path
        self.exit_state = None

        # write workerに対するコマンドqueue
        self.__send_queue = Queue()

        # 最後にエンジン側から受信した行
        self.last_received_line = None

        # 実行ファイルの存在するフォルダ
        self.engine_fullpath = os.path.join(os.getcwd() , self.engine_path)
        self.engine_state = UsiEngineState.WaitConnecting

        # subprocess.Popen()では接続失敗を確認する手段がないくさいので、
        # 事前に実行ファイルが存在するかを調べる。
        if not os.path.exists(self.engine_fullpath):
            self.engine_state = UsiEngineState.Disconnected
            self.exit_state = "Connection Error"
            raise FileNotFoundError(self.engine_fullpath + " not found.")

        self.__proc = subprocess.Popen(self.engine_fullpath , shell=True, \
            stdout=subprocess.PIPE, stderr=subprocess.PIPE , stdin = subprocess.PIPE , \
            encoding = 'utf-8' , cwd=os.path.dirname(self.engine_fullpath))

        # self.send_command("usi")     # "usi"コマンドを先行して送っておく。
        # →　オプション項目が知りたいわけでなければエンジンに対して"usi"、送る必要なかったりする。
        # また、オプション自体は、"engine_options.txt"で設定されるものとする。

        self.engine_state = UsiEngineState.Connected

        # 読み書きスレッド
        self.__read_thread = threading.Thread(target=self.__read_worker)
        self.__read_thread.start()
        self.__write_thread = threading.Thread(target=self.__write_worker)
        self.__write_thread.start()


    # エンジン用のプロセスにコマンドを送信する(プロセスの標準入力にメッセージを送る)
    def send_command(self, message):
        self.__send_queue.put(message)


    # エンジン用のプロセスを終了する
    def disconnect(self):
        if self.__proc is not None:
            self.send_command("quit")
            # スレッドをkillするのはpythonでは難しい。
            # エンジンが行儀よく動作することを期待するしかない。
            # "quit"メッセージを送信して、エンジン側に終了してもらうしかない。

        if self.__read_thread is not None:
            self.__read_thread.join()
            self.__read_thread = None

        if self.__write_thread is not None:
            self.__write_thread.join()
            self.__write_thread = None

        # GCが呼び出されたときに回収されるはずだが、UnitTestでresource leakの警告が出るのが許せないので
        # この時点でclose()を呼び出しておく。
        if self.__proc is not None:
            self.__proc.stdin.close()
            self.__proc.stdout.close()
            self.__proc.stderr.close()
            self.__proc.terminate()

        self.__proc = None
        self.engine_state = UsiEngineState.Disconnected


    # 指定したUsiEngineStateになるのを待つ
    # disconnectedになってしまったら例外をraise
    def wait_for_state(self,state):
        if type(state) is not UsiEngineState:
            raise TypeError("state must be UsiEngineState.")
        while True:
            if  self.engine_state == state:
                return
            if self.engine_state == UsiEngineState.Disconnected:
                raise ValueError("engine_state == UsiEngineState.Disconnected.")
            time.sleep(0.001)


    # 局面をエンジンに送信する。sfen形式。
    # 例 : "startpos moves ..."とか"sfen ... moves ..."みたいな形式 
    # 「USIプロトコル」でググれ。
    def send_position(self,sfen):
        self.wait_for_state(UsiEngineState.WaitCommand)
        self.send_command("position " + sfen)


    # position_command()で設定した局面に対する合法手の指し手の集合を得る。
    # USIプロトコルでの表記文字列で返ってくる。
    # すぐに返ってくるはずなのでブロッキングメソッド
    # "moves"は、やねうら王でしか使えないUSI拡張コマンド
    def get_moves(self):
        self.wait_for_state(UsiEngineState.WaitCommand)
        self.__last_received_line = None
        self.send_command("moves")

        # エンジン側から一行受信するまでblockingして待機
        while True:
            if self.__last_received_line is not None:
                return self.__last_received_line
            time.sleep(0.001)

        # コマンドをエンジンに1行送って1行受け取るだけなのでself.engine_stateは変更しない。


    # position_command()のあと、エンジンに思考させる。
    # options :
    #  "infinite" : stopを送信するまで無限に考える。
    #  "btime 10000 wtime 10000 byoyomi 3000" : 先手、後手の持ち時間 + 秒読みの持ち時間を指定して思考させる。単位は[ms]
    #  "depth 10" : 深さ10固定で思考させる
    def go_command(self,options):
        self.send_command("go " + options)


    # エンジンとのやりとりを行うスレッド(read方向)
    def __read_worker(self):
        while (True):
            line = self.__proc.stdout.readline()
            # プロセスが終了した場合、line = Noneのままreadline()を抜ける。
            if line :
                self.__dispatch_message(line.strip())

            # プロセスが生きているかのチェック
            retcode = self.__proc.poll()
            if not line and retcode is not None:
                self.exit_state = 0
                # エラー以外の何らかの理由による終了
                break


    # エンジンとやりとりを行うスレッド(write方向)
    def __write_worker(self):

        if self.__options is not None:
            for k,v in self.__options.items():
                self.send_command("setoption name {0} value {1}".format(k,v))

        self.send_command("isready") # 先行して"isready"を送信
        self.engine_state = UsiEngineState.WaitReadyOk

        try:
            while(True):
                message = self.__send_queue.get()
                self.__proc.stdin.write(message + '\n')
                self.__proc.stdin.flush()
                if self.debug_print:
                    print("[<] " + message)

                # 終了コマンドを送信したなら自発的にこのスレッドを終了させる。
                if message == "quit":
                    self.engine_state = UsiEngineState.Disconnected
                    break

                retcode = self.__proc.poll()
                if retcode is not None:
                    break
                
        except:
            # print("write worker exception")
            self.exit_state = "Engine error write_worker failed , EngineFullPath = " + self.engine_fullpath


    # エンジン側から送られてきたメッセージを解釈する。
    def __dispatch_message(self,message):
        # デバッグ用に受け取ったメッセージを出力するのか？
        if self.debug_print or (self.error_print and message.find("Error") > -1):
            print("[>] " + message)

        # 最後に受信した文字列はここに積む約束になっている。
        self.__last_received_line = message

        # 先頭の文字列で判別する。
        commands = message.split()
        if len(commands) :
            command = commands[0]
        else:
            command = ""

        if command == "readyok":
            self.engine_state = UsiEngineState.WaitCommand

        # TODO : あとで実装する。


    # デストラクタで通信の切断を行う。
    def __del__(self):
        self.disconnect()


    # === private members ===

    # エンジンのプロセスハンドル
    __proc = None

    # エンジンとやりとりするスレッド
    __read_thread = None
    __write_thread = None

    # エンジンに設定するオプション項目。(dictで)
    # 例 : {"Hash":"128","Threads":"8"}
    __options = None

    # 最後にエンジン側から受信した1行
    __last_received_line = None


if __name__ == "__main__":
    # テスト用のコード
    usi = UsiEngine()
    usi.connect("exe/YaneuraOu.exe")
    print(usi.engine_path)
    usi.send_position("startpos moves 7g7f")
    print("moves = " + usi.get_moves())
    usi.disconnect()
    print(usi.engine_state)
    print(usi.exit_state)
