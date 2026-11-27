import { requireChatGPTUser } from "../chatgpt-auth";
import Workspace from "../workspace";
import {speechExecution} from "@/lib/runtime";
import {processingConfig} from "@/lib/processing";
export const dynamic="force-dynamic";
export const metadata={title:"STT · 회의록 작업실",description:"파일과 녹음에서 녹취만 만들고 TXT로 내려받습니다."};
export default async function Stt(){const user=await requireChatGPTUser("/stt");return <Workspace displayName={user.displayName} mode="transcript" initialConfig={processingConfig(speechExecution())}/>;}
